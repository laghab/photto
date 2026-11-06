package io.github.laghab.photto

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** On-device OCR jobs. Results and cache remain in the folder's app-private index. */
class TextJobs(private val root: File, private val cacheFile: File, private val recursive: Boolean = true) {
    private val lock = Any()
    private val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
    private val executor = Executors.newFixedThreadPool(2)
    private val jobs = HashMap<String, Job>()
    private val db: android.database.sqlite.SQLiteDatabase
    private data class Job(val id: String, val cancel: AtomicBoolean, val matches: JSONArray = JSONArray(),
                           val errors: JSONArray = JSONArray(), var total: Int = 0, var done: Int = 0,
                           var cacheHits: Int = 0, var state: String = "running")

    init {
        cacheFile.parentFile?.mkdirs()
        db = android.database.sqlite.SQLiteDatabase.openOrCreateDatabase(cacheFile, null)
        db.execSQL("CREATE TABLE IF NOT EXISTS ocr_cache(path TEXT,size INTEGER,mtime INTEGER,language TEXT,engine TEXT,sampling TEXT,text TEXT,samples TEXT,PRIMARY KEY(path,size,mtime,language,engine,sampling))")
    }

    fun start(language: String, threshold: Int, query: String, images: Boolean, videos: Boolean): JSONObject {
        val id = UUID.randomUUID().toString()
        val files = ArrayList<File>()
        fun walk(dir: File) {
            dir.listFiles()?.forEach { f ->
                if (f.isDirectory) { if (recursive && !f.name.startsWith(".") && f.name != "SIFT_docs") walk(f) }
                else if (f.isFile && ((images && f.extension.lowercase() in IMAGE_EXT) ||
                            (videos && f.extension.lowercase() in VIDEO_EXT))) files.add(f)
            }
        }
        walk(root)
        val job = Job(id, AtomicBoolean(false), total = files.size)
        synchronized(lock) {
            jobs.values.filter { it.state == "running" }.forEach { it.cancel.set(true) }
            jobs[id] = job
        }
        executor.execute {
            try {
                for (f in files) {
                    if (job.cancel.get()) break
                    val rel = f.relativeTo(root).invariantSeparatorsPath
                    val video = f.extension.lowercase() in VIDEO_EXT
                    try {
                        val sampling = if (video) "1/2/5s" else "none"
                        val cached = synchronized(lock) { db.rawQuery("SELECT samples FROM ocr_cache WHERE path=? AND size=? AND mtime=? AND language=? AND engine=? AND sampling=?",
                            arrayOf(rel, f.length().toString(), f.lastModified().toString(), language, ENGINE, sampling)).use { if (it.moveToFirst()) it.getString(0) else null } }
                        if (cached != null) synchronized(lock) { job.cacheHits++ }
                        val frames = if (cached != null) {
                            val cachedArray = JSONArray(cached)
                            (0 until cachedArray.length()).map { i -> val row = cachedArray.getJSONObject(i); (if (row.isNull("timestamp")) null else row.getInt("timestamp")) to row.getString("text") }
                        } else if (video) videoFrames(f, job.cancel) else listOf(null to recognize(BitmapFactory.decodeFile(f.path)))
                        val text = frames.joinToString("\n") { it.second }
                        if (cached == null && !job.cancel.get()) {
                            val samples = JSONArray(); frames.forEach { (sec, txt) -> samples.put(JSONObject().put("timestamp", sec ?: JSONObject.NULL).put("text", txt)) }
                            synchronized(lock) { db.execSQL("INSERT OR REPLACE INTO ocr_cache VALUES(?,?,?,?,?,?,?,?)",
                                arrayOf(rel, f.length(), f.lastModified(), language, ENGINE, sampling, text, samples.toString())) }
                        }
                        val matchWhy = reason(text, threshold, query)
                        if (matchWhy != null) {
                            val timestamp = frames.firstOrNull { it.first != null &&
                                (reason(it.second, threshold, query) != null || (matchWhy == "threshold" && countWords(it.second) > 0)) }?.first
                            synchronized(lock) { job.matches.put(JSONObject().put("path", rel).put("text", text.take(4000)).put("reason", matchWhy).put("timestamp", timestamp ?: JSONObject.NULL)) }
                        }
                    } catch (e: Exception) {
                        synchronized(lock) { job.errors.put(JSONObject().put("path", rel).put("error", e.message ?: "OCR failed").put("kind", if (video) "decode" else "ocr")) }
                    } finally { synchronized(lock) { job.done++ } }
                }
                synchronized(lock) { job.state = if (job.cancel.get()) "canceled" else "done" }
            } catch (e: Exception) { synchronized(lock) { job.errors.put(JSONObject().put("error", e.message ?: "OCR failed").put("kind", "engine")); job.state = "error" } }
        }
        return JSONObject().put("jobId", id).put("total", files.size)
    }

    private fun recognize(bitmap: Bitmap?): String {
        bitmap ?: throw IllegalArgumentException("Image could not be decoded")
        return try { Tasks.await(recognizer.process(InputImage.fromBitmap(bitmap, 0))).text } finally { bitmap.recycle() }
    }

    private fun videoFrames(file: File, cancel: AtomicBoolean): List<Pair<Int?, String>> {
        val retriever = MediaMetadataRetriever()
        val frames = ArrayList<Pair<Int?, String>>()
        try {
            retriever.setDataSource(file.absolutePath)
            val duration = (retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L) / 1000.0
            if (duration <= 0) throw IllegalArgumentException("Video duration unavailable")
            val step = sampleInterval(duration)
            var sec = 0
            while (sec < duration && !cancel.get()) {
                val bmp = retriever.getFrameAtTime(sec * 1_000_000L, MediaMetadataRetriever.OPTION_CLOSEST)
                if (bmp != null) frames.add(sec to recognize(bmp))
                sec += step
            }
            if (frames.isEmpty() && !cancel.get()) throw IllegalArgumentException("Video decoder could not read any frames")
        } finally { retriever.release() }
        return frames
    }

    private fun countWords(text: String) = meaningfulWordCount(text)
    private fun reason(text: String, threshold: Int, query: String) = matchReason(text, threshold, query)

    fun status(id: String) = synchronized(lock) {
        val j = jobs[id] ?: return@synchronized JSONObject()
        JSONObject().put("jobId", id).put("state", j.state).put("total", j.total).put("done", j.done)
            .put("matches", j.matches.length()).put("cacheHits", j.cacheHits).put("errors", JSONArray(j.errors.toString()))
    }
    fun results(id: String, offset: Int, limit: Int): JSONObject = synchronized(lock) {
        val j = jobs[id] ?: return@synchronized JSONObject().put("items", JSONArray()).put("total", 0).put("offset", 0).put("limit", 0).put("nextOffset", JSONObject.NULL)
        val start = offset.coerceAtLeast(0).coerceAtMost(j.matches.length())
        val size = limit.coerceIn(1, 100)
        val items = JSONArray(); for (i in start until minOf(start + size, j.matches.length())) items.put(j.matches.getJSONObject(i))
        JSONObject().put("items", items).put("total", j.matches.length()).put("offset", start).put("limit", size)
            .put("nextOffset", if (start + size < j.matches.length()) start + size else JSONObject.NULL)
    }
    fun cancel(id: String) = synchronized(lock) { jobs[id]?.cancel?.set(true); jobs.containsKey(id) }
    fun clear() { synchronized(lock) { db.execSQL("DELETE FROM ocr_cache") } }

    fun preview(id: String, paths: JSONArray): JSONArray {
        val allowed = synchronized(lock) { jobs[id]?.matches?.let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("path") }.toSet() } ?: emptySet() }
        val out = JSONArray()
        for (i in 0 until paths.length()) {
            val rel = paths.optString(i)
            if (rel !in allowed) continue
            val source = File(root, rel)
            val base = File(File(root, "SIFT_docs"), rel)
            var dest = base; var n = 1
            while (dest.exists()) { dest = File(base.parentFile, "${source.nameWithoutExtension}_sifted${n++}.${source.extension}") }
            out.put(JSONObject().put("path", rel).put("destination", dest.relativeTo(root).invariantSeparatorsPath))
        }
        return out
    }

    fun action(id: String, paths: JSONArray, op: String): JSONObject {
        require(op == "copy" || op == "move")
        val allowed = synchronized(lock) { jobs[id]?.matches?.let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("path") }.toSet() } ?: emptySet() }
        val out = JSONArray()
        for (i in 0 until paths.length()) {
            val rel = paths.optString(i); val source = File(root, rel).canonicalFile
            var tempFile: File? = null
            try {
                require(rel in allowed && source.path.startsWith(root.canonicalPath + File.separator) && source.isFile) { "Source missing or not a result" }
                val parent = File(root, "SIFT_docs/${File(rel).parent ?: ""}").apply { mkdirs() }
                var dest = File(parent, source.name); var n = 1
                while (dest.exists()) { dest = File(parent, "${source.nameWithoutExtension}_sifted$n.${source.extension}"); n++ }
                val tmp = File(parent, ".${dest.name}.${UUID.randomUUID()}.tmp"); tempFile = tmp
                if (op == "move" && source.renameTo(tmp)) Unit else source.copyTo(tmp)
                while (true) {
                    try { java.nio.file.Files.move(tmp.toPath(), dest.toPath()); break }
                    catch (_: java.nio.file.FileAlreadyExistsException) {
                        dest = File(parent, "${source.nameWithoutExtension}_sifted${n++}.${source.extension}")
                    }
                }
                if (op == "move" && source.exists()) check(source.delete()) { "Destination saved but source could not be removed" }
                out.put(JSONObject().put("path", rel).put("destination", dest.relativeTo(root).invariantSeparatorsPath).put("ok", true))
            } catch (e: Exception) {
                if (op == "move" && !source.exists() && tempFile?.exists() == true) tempFile.renameTo(source)
                out.put(JSONObject().put("path", rel).put("ok", false).put("error", e.message ?: "File operation failed"))
            }
        }
        val succeeded = (0 until out.length()).count { out.getJSONObject(it).optBoolean("ok") }
        return JSONObject().put("results", out).put("copied", if (op == "copy") succeeded else 0).put("moved", if (op == "move") succeeded else 0)
            .put("failed", out.length() - succeeded)
    }

    fun close() {
        synchronized(lock) { jobs.values.forEach { it.cancel.set(true) } }
        executor.shutdownNow()
        executor.awaitTermination(2, TimeUnit.SECONDS)
        synchronized(lock) { recognizer.close(); db.close() }
    }
    companion object {
        private const val ENGINE = "mlkit-latin-bundled-16.0.1"
        private val IMAGE_EXT = setOf("jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "heic", "gif")
        private val VIDEO_EXT = setOf("mp4", "mkv", "mov", "avi", "3gp", "webm")
        internal fun sampleInterval(duration: Double): Int = if (duration < 15) 1 else if (duration < 60) 2 else 5
        private fun normalize(text: String) = text.uppercase().lowercase().replace(Regex("\\s+"), " ").trim()
        internal fun meaningfulWordCount(text: String) = Regex("\\p{L}{3,}").findAll(text).count()
        internal fun matchReason(text: String, threshold: Int, query: String): String? {
            if (query.isNotBlank()) return if (normalize(text).contains(normalize(query))) "query" else null
            return if (meaningfulWordCount(text) >= threshold) "threshold" else null
        }
    }
}
