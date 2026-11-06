package io.github.laghab.photto

import android.content.pm.ApplicationInfo
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.ImageDecoder
import android.graphics.pdf.PdfRenderer
import android.media.MediaMetadataRetriever
import android.media.ThumbnailUtils
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.util.Size
import android.webkit.MimeTypeMap
import io.github.laghab.photto.MiniServer.Request
import io.github.laghab.photto.MiniServer.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.file.Files
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest
import java.util.Collections
import java.util.concurrent.atomic.AtomicBoolean
import java.util.zip.ZipFile
import kotlin.math.max
import kotlin.math.min

/** Android implementation of the SIFT HTTP API shared with the desktop UI. */
class Api(private val act: MainActivity, private val token: String) {

    companion object {
        const val VERSION = "1.1.0"
        const val MAX_FILES = 100_000
        const val SCAN_BATCH = 250
        val IMAGE = setOf("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "ico", "jfif")
        val IMAGE_CONVERT = setOf("heic", "heif", "dng", "tif", "tiff", "cr2", "nef", "arw", "raf", "orf", "rw2", "jxl")
        val VIDEO = setOf("mp4", "webm", "mkv", "mov", "m4v", "avi", "ogv", "3gp", "wmv", "flv", "mpg", "mpeg", "ts")
        val AUDIO = setOf("mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "wma", "amr")
        val TEXT = setOf("txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "yaml", "yml", "toml", "ini",
            "cfg", "conf", "xml", "html", "htm", "css", "js", "ts", "tsx", "jsx", "py", "rs", "go", "c", "h", "cpp",
            "hpp", "java", "kt", "rb", "php", "sh", "bash", "lua", "sql", "tex", "srt", "vtt", "vcf", "nomedia")
        val DOC = setOf("doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "epub")
        val ARCHIVE = setOf("zip", "tar", "gz", "tgz", "xz", "bz2", "zst", "7z", "rar", "iso", "apk", "apks", "xapk")
        val SKIP_DIRS = setOf(".git", "node_modules", "__pycache__", ".thumbnails", ".Trash", "LOST.DIR", "SIFT_docs", "photto trash", MainActivity.TRASH_DIR)
        val STATIC_TYPES = mapOf("html" to "text/html; charset=utf-8", "js" to "text/javascript", "css" to "text/css")
    }

    private val ctx = act.applicationContext
    private val storageRoot: File = Environment.getExternalStorageDirectory()
    private val cacheDir = File(ctx.cacheDir, "previews").apply { mkdirs() }
    private val stateDir = File(ctx.filesDir, "state").apply { mkdirs() }
    private val indexDir = File(ctx.filesDir, "index").apply { mkdirs() }
    private val prefs = ctx.getSharedPreferences("photto", 0)
    private val pdfLock = Any()
    private val debug = (act.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    private val thumbs = ThumbPipeline(ctx, cacheDir) { f, target -> generateThumb(f, target) }
    private val perf = Perf(debug, thumbs)

    @Volatile var session: Session? = null
        set(value) {
            val old = field
            field = value
            if (old != null && old !== value) old.close()
        }

    // ------------------------------------------------------------ session --

    inner class Session(val root: File, val recursive: Boolean) {
        private val stateFile = File(stateDir, sha1(root.absolutePath).take(16) + ".json")
        private val decisions = LinkedHashMap<String, String>()
        private val decisionsLock = Any()
        private val scanLock = Any()
        private val scanCond = Object()
        private val scanErrors = Collections.synchronizedList(ArrayList<String>())
        private val random = java.util.Random()
        val index = FolderIndex(File(indexDir, sha1(root.absolutePath).take(16) + ".sqlite3"))
        val textJobs = TextJobs(root, File(indexDir, sha1(root.absolutePath).take(16) + ".text.sqlite3"), recursive)

        @Volatile private var scanThread: Thread? = null
        @Volatile private var scanCancel = AtomicBoolean(false)
        @Volatile private var closed = false

        init {
            loadDecisions()
            index.applyDecisions(decisionSnapshot())
            addRecent(root.absolutePath)
            startScan()
        }

        private fun loadDecisions() {
            if (!stateFile.exists()) return
            try {
                val d = JSONObject(stateFile.readText()).optJSONObject("decisions") ?: return
                synchronized(decisionsLock) {
                    val keys = d.keys()
                    while (keys.hasNext()) {
                        val k = keys.next()
                        decisions[k] = d.getString(k)
                    }
                }
            } catch (_: Exception) {
            }
        }

        fun decisionSnapshot(): Map<String, String> = synchronized(decisionsLock) { LinkedHashMap(decisions) }

        private fun saveLocked() {
            val d = JSONObject()
            decisions.forEach { (k, v) -> d.put(k, v) }
            try {
                val tmp = File(stateFile.path + ".tmp")
                tmp.writeText(JSONObject().put("root", root.absolutePath).put("decisions", d).toString())
                if (!tmp.renameTo(stateFile)) {
                    stateFile.delete()
                    tmp.renameTo(stateFile)
                }
            } catch (_: Exception) {
            }
        }

        fun setDecision(rel: String, decision: String?) {
            synchronized(decisionsLock) {
                if (decision == null) decisions.remove(rel) else decisions[rel] = decision
                saveLocked()
            }
            index.setDecision(rel, decision)
        }

        fun clearDecisions() {
            synchronized(decisionsLock) {
                decisions.clear()
                saveLocked()
            }
            index.applyDecisions(emptyMap())
        }

        fun committedDeletes(rels: Collection<String>) {
            synchronized(decisionsLock) {
                for (rel in rels) decisions.remove(rel)
                saveLocked()
            }
            index.removeRows(rels)
        }

        fun applyFavoriteMove(oldRel: String, newRel: String) {
            synchronized(decisionsLock) {
                decisions.remove(oldRel)
                decisions[newRel] = "favorite"
                saveLocked()
            }
            index.removeRows(listOf(oldRel))
            index.setDecision(newRel, "favorite")
        }

        fun resolve(rel: String): File? {
            val f = File(root, rel).canonicalFile
            val base = root.canonicalPath
            return if (f.path == base || f.path.startsWith("$base/")) f else null
        }

        // -------------------------------------------------------- scanning --
        fun startScan(refresh: Boolean = false) {
            synchronized(scanLock) {
                if (closed) return
                val current = scanThread
                if (current != null && current.isAlive) {
                    if (!refresh) return
                    scanCancel.set(true)
                    current.join(1500)
                }
                val cancel = AtomicBoolean(false)
                scanCancel = cancel
                scanErrors.clear()
                val generation = index.beginScan()
                perf.markScanStart()
                val t = Thread({ scanWorker(generation, cancel) }, "photto-scan")
                t.isDaemon = true
                scanThread = t
                t.start()
            }
        }

        fun cancelScan(reason: String) {
            synchronized(scanLock) {
                scanCancel.set(true)
                scanThread?.takeIf { it.isAlive }?.join(1500)
            }
            if (index.scanInfo().optString("state") == "scanning") {
                index.setScanState(if (reason == "permission") "permission" else "error", reason)
            }
        }

        private fun notifyScan() {
            synchronized(scanCond) { scanCond.notifyAll() }
        }

        private fun scanWorker(generation: Int, cancel: AtomicBoolean) {
            val batch = ArrayList<FolderIndex.FileRow>(SCAN_BATCH)
            var seen = 0
            var truncated = false
            var firstBatchMarked = false
            fun flush(): Boolean {
                if (!index.upsertBatch(batch, generation, seen, decisionSnapshot())) return false
                if (!firstBatchMarked) {
                    perf.markFirstIndexBatch()
                    firstBatchMarked = true
                }
                batch.clear()
                notifyScan()
                return true
            }
            try {
                val stack = ArrayDeque<File>().apply { add(root) }
                outer@ while (stack.isNotEmpty()) {
                    if (cancel.get() || index.currentGeneration() != generation) return
                    val dir = stack.removeLast()
                    val list = try { dir.listFiles() } catch (_: Exception) { null }
                    if (list == null) {
                        scanErrors.add("Can't read ${dir.absolutePath}")
                        continue
                    }
                    list.sortBy { it.name.lowercase() }
                    for (f in list) {
                        if (cancel.get() || index.currentGeneration() != generation) return
                        val name = f.name
                        if (name.startsWith(".")) continue
                        val attrs = try {
                            Files.readAttributes(f.toPath(), BasicFileAttributes::class.java)
                        } catch (_: Exception) {
                            continue
                        }
                        if (attrs.isSymbolicLink) continue
                        if (attrs.isDirectory) {
                            val androidDir = name == "Android" && dir.absolutePath == storageRoot.absolutePath
                            if (recursive && name !in SKIP_DIRS && !androidDir) stack.add(f)
                            continue
                        }
                        if (!attrs.isRegularFile) continue
                        if (seen >= MAX_FILES) {
                            truncated = true
                            break@outer
                        }
                        val rel = f.relativeTo(root).path
                        val parent = File(rel).parent ?: ""
                        val kind = kindOf(f)
                        batch.add(FolderIndex.FileRow(
                            rel, name, parent, f.extension.lowercase(), kind,
                            attrs.size(), attrs.lastModifiedTime().toMillis() / 1000.0,
                            FolderIndex.sortName(name), random.nextDouble(), generation,
                        ))
                        seen++
                        if (batch.size >= SCAN_BATCH && !flush()) return
                    }
                }
                if (!flush()) return
                val gone = index.finishScan(generation, seen, truncated)
                cleanStaleDecisions(gone)
                index.applyDecisions(decisionSnapshot())
                if (scanErrors.isNotEmpty()) {
                    index.setScanError(scanErrors.take(3).joinToString("; "))
                }
                perf.markIndexComplete()
            } catch (t: Throwable) {
                index.failScan(generation, t.message ?: t.toString(), seen)
            } finally {
                notifyScan()
            }
        }

        private fun cleanStaleDecisions(gone: List<String>) {
            synchronized(decisionsLock) {
                val stale = ArrayList<String>()
                for (rel in decisions.keys) if (!File(root, rel).isFile) stale.add(rel)
                for (rel in gone) if (decisions.containsKey(rel) && rel !in stale) stale.add(rel)
                if (stale.isEmpty()) return
                for (rel in stale) decisions.remove(rel)
                saveLocked()
            }
        }

        /** A cold page waits briefly for the first batch; it never waits for the full scan. */
        private fun waitForRows(timeoutMs: Long) {
            if (index.fileCount() > 0 || index.scanInfo().optString("state") != "scanning") return
            val deadline = System.currentTimeMillis() + timeoutMs
            synchronized(scanCond) {
                while (index.fileCount() == 0 && index.scanInfo().optString("state") == "scanning" &&
                    System.currentTimeMillis() < deadline) {
                    try {
                        scanCond.wait(50)
                    } catch (_: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return
                    }
                }
            }
        }

        fun page(cursor: String = "", offset: Int = 0, limit: Int = FolderIndex.DEFAULT_PAGE_SIZE,
                 sort: String = "shuffle", group: String = "all", decision: String = "none"): JSONObject {
            waitForRows(2000)
            return index.page(cursor, offset, limit, sort, group, decision)
        }

        fun scanPayload(): JSONObject = JSONObject()
            .put("revision", index.revision())
            .put("scan", index.scanInfo())
            .put("counts", index.counts())
            .put("truncated", index.truncated())

        fun close() {
            synchronized(scanLock) {
                if (closed) return
                closed = true
                scanCancel.set(true)
                scanThread?.takeIf { it.isAlive }?.join(1500)
            }
            index.close()
            textJobs.close()
        }
    }

    fun onPermissionLost() {
        session?.cancelScan("permission")
    }

    fun close() {
        session?.close()
        session = null
        thumbs.close()
    }

    // -------------------------------------------------------------- utils --

    private fun loadRecent(): List<String> = try {
        val a = JSONArray(prefs.getString("recent", "[]"))
        (0 until a.length()).map { a.getString(it) }.filter { File(it).isDirectory }
    } catch (_: Exception) { emptyList() }

    private fun addRecent(path: String) {
        val items = listOf(path) + loadRecent().filter { it != path }
        prefs.edit().putString("recent", JSONArray(items.take(10)).toString()).apply()
    }

    private fun sha1(s: String) =
        MessageDigest.getInstance("SHA-1").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }

    fun kindOf(f: File): String {
        val e = f.extension.lowercase()
        return when {
            e in IMAGE -> "image"
            e in IMAGE_CONVERT -> "image-convert"
            e in VIDEO -> "video"
            e in AUDIO -> "audio"
            e == "pdf" -> "pdf"
            e in TEXT -> "text"
            e.isEmpty() && f.length() < 512_000 && looksText(f) -> "text"
            e in DOC -> "doc"
            e in ARCHIVE -> "archive"
            else -> "other"
        }
    }

    private fun looksText(f: File) = try {
        f.inputStream().use { s -> val b = ByteArray(2048); val n = s.read(b); n <= 0 || (0 until n).none { b[it] == 0.toByte() } }
    } catch (_: Exception) { false }

    private fun human(n: Long): String {
        var v = n.toDouble(); var i = 0
        val u = arrayOf("B", "KB", "MB", "GB", "TB")
        while (v >= 1024 && i < u.size - 1) { v /= 1024; i++ }
        return if (i == 0) "$n B" else "%.1f %s".format(v, u[i])
    }

    private fun mimeOf(f: File): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.extension.lowercase()) ?: when (f.extension.lowercase()) {
            "mkv" -> "video/x-matroska"; "opus" -> "audio/ogg"; "svg" -> "image/svg+xml"
            else -> "application/octet-stream"
        }

    private fun cacheFile(f: File, tag: String, ext: String) =
        File(cacheDir, sha1("${f.absolutePath}|${f.lastModified()}|${f.length()}|$tag") + ".$ext")

    private fun saveJpeg(bmp: Bitmap, out: File, quality: Int = 82): File {
        val tmp = File(out.path + ".tmp")
        tmp.outputStream().use { bmp.compress(Bitmap.CompressFormat.JPEG, quality, it) }
        if (!tmp.renameTo(out)) {
            out.delete()
            tmp.renameTo(out)
        }
        return out
    }

    // ----------------------------------------------------------- previews --

    private fun decodeImage(f: File, target: Int): Bitmap? = try {
        ImageDecoder.decodeBitmap(ImageDecoder.createSource(f)) { decoder, info, _ ->
            val w = info.size.width
            val h = info.size.height
            if (w > 0 && h > 0 && (w > target || h > target)) {
                val scale = target.toDouble() / max(w, h)
                decoder.setTargetSize(max(1, (w * scale).toInt()), max(1, (h * scale).toInt()))
            }
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
        }
    } catch (_: Throwable) {
        null
    }

    private fun generateThumb(f: File, target: Int): Bitmap? = when (kindOf(f)) {
        "image", "image-convert" -> decodeImage(f, target)
        "video" -> try { ThumbnailUtils.createVideoThumbnail(f, Size(target, target), null) } catch (_: Exception) { null }
        "pdf", "doc" -> renderPdf(f, 1, target)
        "audio" -> try { ThumbnailUtils.createAudioThumbnail(f, Size(target, target), null) } catch (_: Exception) { null }
        else -> null
    }

    private fun viewable(f: File): File? {
        if (kindOf(f) != "image-convert") return f
        val out = cacheFile(f, "view", "jpg")
        if (out.exists()) return out
        return try {
            val bmp = ImageDecoder.decodeBitmap(ImageDecoder.createSource(f)) { dec, info, _ ->
                val longest = maxOf(info.size.width, info.size.height)
                if (longest > 2400) dec.setTargetSampleSize(Math.ceil(longest / 2400.0).toInt())
                dec.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
            }
            saveJpeg(bmp, out, 88).also { bmp.recycle() }
        } catch (_: Exception) { null }
    }

    private fun pdfPageCount(f: File): Int = if (f.extension.lowercase() != "pdf") 0 else synchronized(pdfLock) {
        try {
            ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY).use { fd -> PdfRenderer(fd).use { it.pageCount } }
        } catch (_: Exception) { 0 }
    }

    private fun renderPdf(f: File, n: Int, width: Int): Bitmap? = if (f.extension.lowercase() != "pdf") null else
        synchronized(pdfLock) {
            ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
                PdfRenderer(fd).use { r ->
                    if (n < 1 || n > r.pageCount) return@use null
                    r.openPage(n - 1).use { page ->
                        val h = (width.toLong() * page.height / page.width).toInt().coerceAtLeast(1)
                        val bmp = Bitmap.createBitmap(width, h, Bitmap.Config.ARGB_8888)
                        bmp.eraseColor(Color.WHITE)
                        page.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                        bmp
                    }
                }
            }
        }

    private fun mediaInfo(f: File): JSONObject {
        val o = JSONObject()
        val r = MediaMetadataRetriever()
        try {
            r.setDataSource(f.absolutePath)
            r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull()?.let { o.put("duration", it / 1000.0) }
            val w = r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH)
            val h = r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT)
            if (w != null && h != null) o.put("resolution", "$w×$h")
            r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_TITLE)?.let { o.put("title", it) }
            r.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ARTIST)?.let { o.put("artist", it) }
        } catch (_: Exception) {
        } finally { try { r.release() } catch (_: Exception) {} }
        return o
    }

    private fun textOf(f: File): JSONObject {
        if (kindOf(f) == "archive") {
            val names = try { ZipFile(f).use { z -> z.entries().asSequence().map { it.name }.toList() } } catch (_: Exception) { emptyList() }
            val more = if (names.size > 200) "\n… and ${names.size - 200} more" else ""
            return JSONObject().put("text", names.take(200).joinToString("\n") + more).put("truncated", false)
        }
        val buf = ByteArray(minOf(f.length(), 200_000L).toInt())
        f.inputStream().use { s -> var o = 0; while (o < buf.size) { val n = s.read(buf, o, buf.size - o); if (n < 0) break; o += n } }
        return JSONObject().put("text", String(buf, Charsets.UTF_8)).put("truncated", f.length() > 200_000)
    }

    // ------------------------------------------------------------ folders --

    private fun sdCards(): List<File> = ctx.getExternalFilesDirs(null).drop(1).mapNotNull { d ->
        d?.absolutePath?.substringBefore("/Android/")?.let { File(it) }?.takeIf { it.isDirectory }
    }

    private fun dirs(path: String): JSONObject {
        val dir = File(path.ifEmpty { session?.root?.absolutePath ?: storageRoot.absolutePath })
            .let { if (it.isDirectory) it else storageRoot }
        val o = JSONObject().put("path", dir.absolutePath)
        val roots = (listOf(storageRoot) + sdCards()).map { it.absolutePath }
        o.put("parent", if (dir.absolutePath in roots || dir.parentFile == null) JSONObject.NULL else dir.parent)
        val list = JSONArray()
        val subs = dir.listFiles()
        if (subs == null) o.put("error", "Can't open this folder")
        subs?.filter { it.isDirectory && !it.name.startsWith(".") }?.sortedBy { it.name.lowercase() }?.forEach {
            list.put(JSONObject().put("name", it.name).put("path", it.absolutePath))
        }
        o.put("dirs", list)
        val places = JSONArray()
        fun place(name: String, f: File) { if (f.isDirectory) places.put(JSONObject().put("name", name).put("path", f.absolutePath)) }
        place("Phone", storageRoot)
        place("Camera", File(storageRoot, "DCIM/Camera"))
        place("Pictures", File(storageRoot, Environment.DIRECTORY_PICTURES))
        place("Screenshots", File(storageRoot, "Pictures/Screenshots"))
        place("Downloads", File(storageRoot, Environment.DIRECTORY_DOWNLOADS))
        place("Movies", File(storageRoot, Environment.DIRECTORY_MOVIES))
        place("Music", File(storageRoot, Environment.DIRECTORY_MUSIC))
        place("Documents", File(storageRoot, Environment.DIRECTORY_DOCUMENTS))
        place("WhatsApp", File(storageRoot, "Android/media/com.whatsapp/WhatsApp/Media"))
        place("Telegram", File(storageRoot, "Download/Telegram"))
        sdCards().forEachIndexed { i, f -> place(if (i == 0) "SD card" else "SD card ${i + 1}", f) }
        o.put("places", places)
        o.put("recent", JSONArray(loadRecent().take(6).map { JSONObject().put("name", File(it).name).put("path", it) }))
        return o
    }

    // ------------------------------------------------------------- router --

    fun handle(req: Request): Response {
        val path = req.path
        if (path == "/" || path == "/index.html") return asset("index.html")
        if (path.startsWith("/static/")) return asset(path.removePrefix("/static/"))

        val tok = req.headers["x-token"] ?: req.q("t")
        if (tok != token) return Response.text(403, "bad token")
        act.lastPing = System.currentTimeMillis()

        if (path == "/api/debug/perf") {
            if (!debug) return Response.text(404, "not found")
            return if (req.method == "POST") {
                val body = try { JSONObject(String(req.body)) } catch (_: Exception) { JSONObject() }
                perf.acceptClient(body)
                perf.logSummary()
                Response.json(perf.snapshot().toString())
            } else {
                perf.logSummary()
                Response.json(perf.snapshot().toString())
            }
        }

        val s = session
        val open = setOf("/api/files", "/api/dirs", "/api/ping", "/api/root", "/api/pick", "/api/permission")
        if (path == "/api/permission") { act.openAllFilesSettings(); return ok() }
        if (!act.hasAllFilesAccess()) {
            s?.cancelScan("permission")
            return if (path == "/api/files") Response.json(JSONObject().put("needsPermission", true)
                .put("platform", "android").put("files", JSONArray()).toString())
            else Response.text(409, "no permission")
        }
        if (s == null && path !in open) return Response.text(409, "no folder open")
        val body = if (req.body.isNotEmpty()) try { JSONObject(String(req.body)) } catch (_: Exception) { JSONObject() } else JSONObject()

        when (path) {
            "/api/ping" -> return ok()
            "/api/files" -> {
                if (s == null) return Response.json(JSONObject().put("root", JSONObject.NULL).put("needsPick", true)
                    .put("platform", "android").put("files", JSONArray()).put("recent", JSONArray(loadRecent())).toString())
                if (req.q("refresh") == "1") s.startScan(refresh = true)
                val limit = req.q("limit").toIntOrNull() ?: FolderIndex.DEFAULT_PAGE_SIZE
                val offset = req.q("offset").toIntOrNull() ?: 0
                val page = s.page(
                    cursor = req.q("cursor"), offset = offset, limit = limit,
                    sort = req.q("sort").ifEmpty { "shuffle" },
                    group = req.q("group").ifEmpty { "all" },
                    decision = req.q("decision").ifEmpty { "none" },
                )
                val progress = s.scanPayload()
                val counts = progress.getJSONObject("counts")
                if (page.getJSONArray("files").length() > 0) perf.markFirstPage()
                return Response.json(JSONObject()
                    .put("root", s.root.absolutePath).put("name", s.root.name)
                    .put("recursive", s.recursive).put("version", VERSION).put("platform", "android")
                    .put("offset", page.optInt("offset")).put("limit", page.optInt("limit"))
                    .put("effectiveLimit", page.optInt("effectiveLimit"))
                    .put("files", page.getJSONArray("files"))
                    .put("nextCursor", page.get("nextCursor"))
                    .put("revision", progress.getInt("revision"))
                    .put("scan", progress.getJSONObject("scan"))
                    .put("counts", counts).put("truncated", progress.getBoolean("truncated"))
                    .put("total", counts.optInt("total"))
                    .toString())
            }
            "/api/scan" -> {
                val progress = s!!.scanPayload()
                return Response.json(JSONObject()
                    .put("root", s.root.absolutePath).put("name", s.root.name)
                    .put("recursive", s.recursive).put("version", VERSION).put("platform", "android")
                    .put("revision", progress.getInt("revision"))
                    .put("scan", progress.getJSONObject("scan"))
                    .put("counts", progress.getJSONObject("counts"))
                    .put("truncated", progress.getBoolean("truncated"))
                    .toString())
            }
            "/api/text/start" -> {
                val result = s!!.textJobs.start(body.optString("language", "eng"), body.optInt("threshold", 2).coerceIn(0, 100),
                    body.optString("query").take(200), body.optBoolean("images", true), body.optBoolean("videos", true))
                return Response.json(result.toString())
            }
            "/api/text/status" -> return Response.json(s!!.textJobs.status(req.q("jobId")).toString())
            "/api/text/results" -> return Response.json(s!!.textJobs.results(req.q("jobId"), req.q("offset").toIntOrNull() ?: 0,
                req.q("limit").toIntOrNull() ?: 50).toString())
            "/api/text/cancel" -> return Response.json(JSONObject().put("ok", s!!.textJobs.cancel(body.optString("jobId"))).toString())
            "/api/text/clear" -> { s!!.textJobs.clear(); return ok() }
            "/api/text/preview" -> {
                val paths = body.optJSONArray("paths") ?: JSONArray()
                val results = s!!.textJobs.preview(body.optString("jobId"), paths)
                return Response.json(JSONObject().put("destinations", results).toString())
            }
            "/api/text/action" -> {
                val paths = body.optJSONArray("paths") ?: JSONArray()
                return try { Response.json(s!!.textJobs.action(body.optString("jobId"), paths, body.optString("action")).toString()) }
                catch (e: IllegalArgumentException) { Response.text(400, e.message ?: "bad action") }
            }
            "/api/dirs" -> return Response.json(dirs(req.q("path")).toString())
            "/api/root" -> {
                val dir = File(body.optString("path", s?.root?.absolutePath ?: storageRoot.absolutePath))
                if (!dir.isDirectory) return Response.text(400, "not a directory")
                val previous = session
                val next = Session(dir.canonicalFile, body.optBoolean("recursive", true))
                session = next
                // Stop the old folder's walker: its index is separate, so it would
                // otherwise keep hashing files until it finished on its own.
                if (previous !== next) previous?.close()
                return Response.json(JSONObject().put("ok", true).put("root", dir.absolutePath).toString())
            }
            "/api/pick" -> return Response.json("""{"error":"no-dialog"}""")
            "/api/decide" -> {
                val rel = body.optString("path")
                if (rel.isEmpty() || s!!.resolve(rel) == null) return Response.text(400, "bad path")
                val d = body.optString("decision")
                s.setDecision(rel, if (d in setOf("keep", "delete", "favorite")) d else null)
                return ok()
            }
            "/api/reset" -> { s!!.clearDecisions(); return ok() }
            "/api/commit" -> return Response.json(commit(s!!, body).toString())
            "/api/open" -> {
                val f = s!!.resolve(body.optString("path")) ?: return Response.text(404, "not found")
                return if (act.openExternally(f, mimeOf(f))) ok() else Response.text(404, "no app")
            }
        }

        // file routes
        val f = s!!.resolve(req.q("p"))?.takeIf { it.isFile } ?: return Response.text(404, "not found")
        return when (path) {
            "/raw" -> Response(200, mimeOf(f), file = f)
            "/view" -> viewable(f)?.let { Response(200, if (it == f) mimeOf(f) else "image/jpeg", file = it) }
                ?: Response.text(415, "can't convert")
            "/thumb" -> thumbnail(req, f)
            "/text" -> Response.json(textOf(f).toString())
            "/info" -> Response.json(mediaInfo(f).toString())
            "/pdfinfo" -> Response.json(JSONObject().put("pages", pdfPageCount(f)).toString())
            "/pdfpage" -> {
                val n = req.q("n").toIntOrNull() ?: 1
                val out = cacheFile(f, "page$n", "jpg")
                if (!out.exists()) {
                    val bmp = try { renderPdf(f, n, 1400) } catch (_: Exception) { null } ?: return Response.text(404, "no page")
                    saveJpeg(bmp, out, 90); bmp.recycle()
                }
                Response(200, "image/jpeg", file = out)
            }
            else -> Response.text(404, "not found")
        }
    }

    private fun thumbnail(req: Request, f: File): Response {
        val version = req.q("v")
        val priority = req.q("prio").toIntOrNull() ?: 3
        val cache = if (version.isNotEmpty()) "private, max-age=31536000, immutable"
            else "private, max-age=300"
        val kind = kindOf(f)
        if (kind == "image" && f.extension.lowercase() == "svg") {
            return Response(200, "image/svg+xml", file = f, headers = mapOf("Cache-Control" to cache))
        }
        val preview = thumbs.get(f, version, priority) ?: return Response.text(404, "no thumbnail")
        return Response(200, preview.mime, preview.bytes, headers = mapOf("Cache-Control" to cache))
    }

    private fun ok() = Response.json("""{"ok":true}""")

    private fun asset(name: String): Response {
        if (name.contains("..")) return Response.text(404, "not found")
        return try {
            val bytes = ctx.assets.open(name).use { it.readBytes() }
            Response(200, STATIC_TYPES[name.substringAfterLast('.')] ?: "application/octet-stream", bytes,
                headers = mapOf("Cache-Control" to "no-cache"))
        } catch (_: Exception) { Response.text(404, "not found") }
    }

    private fun commit(s: Session, body: JSONObject): JSONObject {
        val trashed = JSONArray(); val failed = JSONArray(); val favorited = JSONArray()
        var freed = 0L
        val doomed = s.decisionSnapshot().filter { it.value == "delete" }.keys.toList()
            .mapNotNull { rel -> s.resolve(rel)?.takeIf { it.isFile }?.let { rel to it } }
        if (doomed.isNotEmpty()) {
            val sizes = doomed.associate { it.first to it.second.length() }
            val outcome = act.trashFiles(doomed.map { it.second })
            val removed = ArrayList<String>()
            for ((rel, f) in doomed) {
                val err = outcome[f]
                if (err == null) {
                    trashed.put(rel); removed.add(rel); freed += sizes[rel] ?: 0
                } else {
                    failed.put(JSONObject().put("path", rel).put("error", err))
                }
            }
            if (removed.isNotEmpty()) s.committedDeletes(removed)
        }
        val favDir = body.optString("favoritesFolder", "")
        if (favDir.isNotEmpty() && favDir != "null") {
            val dest = s.resolve(favDir)
            if (dest != null) {
                dest.mkdirs()
                for ((rel, d) in s.decisionSnapshot()) {
                    if (d != "favorite") continue
                    val f = s.resolve(rel)?.takeIf { it.isFile && it.parentFile != dest } ?: continue
                    var target = File(dest, f.name); var i = 1
                    while (target.exists()) { target = File(dest, "${f.nameWithoutExtension} ($i).${f.extension}"); i++ }
                    if (f.renameTo(target)) {
                        act.rescan(listOf(f, target))
                        val newRel = target.relativeTo(s.root).path
                        s.applyFavoriteMove(rel, newRel)
                        favorited.put(newRel)
                    }
                }
            }
        }
        if (trashed.length() > 0 || favorited.length() > 0) s.startScan(refresh = true)
        return JSONObject().put("trashed", trashed).put("failed", failed).put("favorited", favorited)
            .put("freed", freed).put("freedH", human(freed))
    }
}
