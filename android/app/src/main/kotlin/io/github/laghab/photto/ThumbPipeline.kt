package io.github.laghab.photto

import android.app.ActivityManager
import android.content.Context
import android.graphics.Bitmap
import android.util.LruCache
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.PriorityBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.max
import kotlin.math.min

/**
 * Bounded, prioritized thumbnail generation with a memory LRU and a versioned disk cache.
 *
 * Callers block on [get] until the work is done, but the actual decodes run on at most
 * [workers] threads and never for the same key twice. High-priority work (the top card,
 * visible Review tiles) is taken from the queue before prefetch work.
 */
class ThumbPipeline(
    context: Context,
    private val cacheDir: File,
    private val target: Int = 480,
    private val generate: (File, Int) -> Bitmap?,
) {

    class Preview(val bytes: ByteArray, val mime: String = "image/jpeg")

    private class Job(
        val key: String,
        val file: File,
        val disk: File,
        val priority: Int,
        val seq: Long,
    ) : Comparable<Job> {
        @Volatile var cancelled = false
        @Volatile var result: Preview? = null
        @Volatile var error: String? = null
        val latch = CountDownLatch(1)

        override fun compareTo(other: Job): Int =
            if (priority != other.priority) priority - other.priority else seq.compareTo(other.seq)
    }

    companion object {
        private const val DISK_LIMIT_BYTES = 128L * 1024 * 1024
        private const val MAX_AGE_MS = 30L * 24 * 60 * 60 * 1000
        private const val JPEG_QUALITY = 82
    }

    private val appContext = context.applicationContext
    private val am = appContext.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
    private val memoryKb = ((am.memoryClass * 1024) / 8).coerceIn(4 * 1024, 32 * 1024)
    private val memory = object : LruCache<String, ByteArray>(memoryKb) {
        override fun sizeOf(key: String, value: ByteArray): Int = max(1, value.size / 1024)
    }

    val workers: Int = if (am.isLowRamDevice) 2 else Runtime.getRuntime().availableProcessors().coerceIn(2, 4)
    private val queue = PriorityBlockingQueue<Job>()
    private val jobs = ConcurrentHashMap<String, Job>()
    private val closed = AtomicBoolean(false)
    private val seq = AtomicLong(0)
    private val active = AtomicInteger(0)
    private val maxConcurrent = AtomicInteger(0)
    private val decodes = AtomicInteger(0)
    private val generated = AtomicInteger(0)
    private val memoryHits = AtomicInteger(0)
    private val diskHits = AtomicInteger(0)
    private val failures = AtomicInteger(0)
    private val threads = ArrayList<Thread>(workers)

    init {
        cacheDir.mkdirs()
        prune()
        repeat(workers) { i ->
            val t = Thread({ workLoop() }, "photto-thumb-$i")
            t.isDaemon = true
            t.start()
            threads.add(t)
        }
    }

    private fun workLoop() {
        while (!closed.get()) {
            val job = try {
                queue.take()
            } catch (_: InterruptedException) {
                break
            }
            if (job.cancelled || closed.get()) {
                jobs.remove(job.key, job)
                job.latch.countDown()
                continue
            }
            val now = active.incrementAndGet()
            maxConcurrent.accumulateAndGet(now) { a, b -> max(a, b) }
            try {
                if (job.disk.exists() && job.disk.length() > 0) {
                    val bytes = job.disk.readBytes()
                    memory.put(job.key, bytes)
                    diskHits.incrementAndGet()
                    job.result = Preview(bytes)
                } else {
                    val bmp = generate(job.file, target)
                    if (bmp == null) {
                        job.error = "no thumbnail"
                    } else {
                        val bytes = ByteArrayOutputStream().use { out ->
                            bmp.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
                            out.toByteArray()
                        }
                        bmp.recycle()
                        if (!job.cancelled) {
                            writeAtomically(job.disk, bytes)
                            memory.put(job.key, bytes)
                            generated.incrementAndGet()
                            decodes.incrementAndGet()
                            job.result = Preview(bytes)
                        }
                    }
                }
            } catch (t: Throwable) {
                job.error = t.message ?: t.toString()
                failures.incrementAndGet()
            } finally {
                active.decrementAndGet()
                jobs.remove(job.key, job)
                job.latch.countDown()
            }
        }
    }

    /** Returns the encoded thumbnail, waiting for a worker if it is not cached yet. */
    fun get(file: File, version: String, priority: Int = 3, timeoutSeconds: Long = 30): Preview? {
        val key = keyFor(file, version)
        memory.get(key)?.let {
            memoryHits.incrementAndGet()
            return Preview(it)
        }
        val disk = File(cacheDir, "t-$key.jpg")
        if (disk.exists() && disk.length() > 0) {
            return try {
                val bytes = disk.readBytes()
                memory.put(key, bytes)
                diskHits.incrementAndGet()
                Preview(bytes)
            } catch (_: Exception) {
                null
            }
        }

        val job = synchronized(jobs) {
            jobs[key] ?: Job(key, file, disk, priority.coerceIn(1, 4), seq.incrementAndGet())
                .also {
                    jobs[key] = it
                    queue.put(it)
                }
        }
        return try {
            if (!job.latch.await(timeoutSeconds, TimeUnit.SECONDS)) {
                failure(file)
                null
            } else {
                job.result ?: run {
                    failure(file)
                    null
                }
            }
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            failure(file)
            null
        }
    }

    private fun failure(@Suppress("UNUSED_PARAMETER") file: File) = failures.incrementAndGet()

    /** Drop queued work for a card or tile that left the view. */
    fun cancel(file: File, version: String) {
        val key = keyFor(file, version)
        synchronized(jobs) {
            jobs.remove(key)?.let {
                it.cancelled = true
                it.latch.countDown()
            }
        }
    }

    fun stats(): JSONObject = JSONObject()
        .put("workers", workers)
        .put("queueDepth", queue.size + active.get())
        .put("active", active.get())
        .put("decodes", decodes.get())
        .put("generated", generated.get())
        .put("maxConcurrent", maxConcurrent.get())
        .put("memoryHits", memoryHits.get())
        .put("diskHits", diskHits.get())
        .put("failures", failures.get())
        .put("memoryKb", memoryKb)
        .put("memoryUsedKb", memory.size())

    fun close() {
        if (!closed.compareAndSet(false, true)) return
        queue.clear()
        for (t in threads) t.interrupt()
        threads.clear()
    }

    private fun keyFor(file: File, version: String): String {
        val raw = "${file.absolutePath}|${file.length()}|${file.lastModified()}|$version|$target"
        return MessageDigest.getInstance("SHA-1").digest(raw.toByteArray())
            .joinToString("") { "%02x".format(it) }
    }

    private fun writeAtomically(dest: File, bytes: ByteArray) {
        val tmp = File(dest.path + ".tmp")
        tmp.writeBytes(bytes)
        if (!tmp.renameTo(dest)) {
            dest.delete()
            tmp.renameTo(dest)
        }
    }

    /** Drop stale and excess thumbnails; view/page caches keep their existing files. */
    private fun prune() {
        val cutoff = System.currentTimeMillis() - MAX_AGE_MS
        val files = cacheDir.listFiles { f -> f.isFile && f.name.startsWith("t-") } ?: return
        var total = 0L
        for (f in files) {
            if (f.lastModified() < cutoff) {
                f.delete()
                continue
            }
            total += f.length()
        }
        if (total <= DISK_LIMIT_BYTES) return
        val byAge = files.filter { it.exists() }.sortedBy { it.lastModified() }
        var left = total
        for (f in byAge) {
            if (left <= DISK_LIMIT_BYTES * 3 / 4) break
            val size = f.length()
            if (f.delete()) left -= size
        }
    }
}
