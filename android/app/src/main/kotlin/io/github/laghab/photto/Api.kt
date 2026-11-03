package io.github.laghab.photto

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
import java.security.MessageDigest
import java.util.zip.ZipFile

/** Android port of photto.py's HTTP API. Same routes, same JSON, so the web UI is shared. */
class Api(private val act: MainActivity, private val token: String) {

    companion object {
        const val VERSION = "1.1.0"
        const val MAX_FILES = 100_000
        val IMAGE = setOf("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "ico", "jfif")
        val IMAGE_CONVERT = setOf("heic", "heif", "dng", "tif", "tiff", "cr2", "nef", "arw", "raf", "orf", "rw2", "jxl")
        val VIDEO = setOf("mp4", "webm", "mkv", "mov", "m4v", "avi", "ogv", "3gp", "wmv", "flv", "mpg", "mpeg", "ts")
        val AUDIO = setOf("mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "wma", "amr")
        val TEXT = setOf("txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "yaml", "yml", "toml", "ini",
            "cfg", "conf", "xml", "html", "htm", "css", "js", "ts", "tsx", "jsx", "py", "rs", "go", "c", "h", "cpp",
            "hpp", "java", "kt", "rb", "php", "sh", "bash", "lua", "sql", "tex", "srt", "vtt", "vcf", "nomedia")
        val DOC = setOf("doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "epub")
        val ARCHIVE = setOf("zip", "tar", "gz", "tgz", "xz", "bz2", "zst", "7z", "rar", "iso", "apk", "apks", "xapk")
        val SKIP_DIRS = setOf(".git", "node_modules", "__pycache__", ".thumbnails", ".Trash", "LOST.DIR", MainActivity.TRASH_DIR)
        val STATIC_TYPES = mapOf("html" to "text/html; charset=utf-8", "js" to "text/javascript", "css" to "text/css")
    }

    private val ctx = act.applicationContext
    private val storageRoot: File = Environment.getExternalStorageDirectory()
    private val cacheDir = File(ctx.cacheDir, "previews").apply { mkdirs() }
    private val stateDir = File(ctx.filesDir, "state").apply { mkdirs() }
    private val prefs = ctx.getSharedPreferences("photto", 0)
    private val pdfLock = Any()

    @Volatile var session: Session? = null

    // ------------------------------------------------------------ session --

    inner class Session(val root: File, val recursive: Boolean) {
        private val stateFile = File(stateDir, sha1(root.absolutePath).take(16) + ".json")
        val decisions = LinkedHashMap<String, String>()
        var truncated = false

        init {
            addRecent(root.absolutePath)
            if (stateFile.exists()) try {
                val d = JSONObject(stateFile.readText()).optJSONObject("decisions")
                d?.keys()?.forEach { decisions[it] = d.getString(it) }
            } catch (_: Exception) {}
        }

        @Synchronized fun save() {
            val d = JSONObject(); decisions.forEach { (k, v) -> d.put(k, v) }
            val tmp = File(stateFile.path + ".tmp")
            tmp.writeText(JSONObject().put("root", root.absolutePath).put("decisions", d).toString())
            tmp.renameTo(stateFile)
        }

        fun resolve(rel: String): File? {
            val f = File(root, rel).canonicalFile
            val base = root.canonicalPath
            return if (f.path == base || f.path.startsWith("$base/")) f else null
        }

        fun scan(): JSONArray {
            val out = JSONArray()
            truncated = false
            val stack = ArrayDeque<File>().apply { add(root) }
            outer@ while (stack.isNotEmpty()) {
                val dir = stack.removeLast()
                val list = dir.listFiles() ?: continue
                list.sortBy { it.name.lowercase() }
                for (f in list) {
                    val name = f.name
                    if (name.startsWith(".")) continue
                    if (Files.isSymbolicLink(f.toPath())) continue
                    if (f.isDirectory) {
                        val androidDir = name == "Android" && dir.absolutePath == storageRoot.absolutePath
                        if (recursive && name !in SKIP_DIRS && !androidDir) stack.add(f)
                        continue
                    }
                    if (!f.isFile) continue
                    if (out.length() >= MAX_FILES) { truncated = true; break@outer }
                    val rel = f.relativeTo(root).path
                    val parent = File(rel).parent ?: ""
                    out.put(JSONObject()
                        .put("path", rel).put("name", name).put("dir", parent)
                        .put("ext", f.extension.lowercase()).put("kind", kindOf(f))
                        .put("size", f.length()).put("sizeH", human(f.length()))
                        .put("mtime", f.lastModified() / 1000.0)
                        .put("decision", decisions[rel] ?: JSONObject.NULL))
                }
            }
            synchronized(this) {
                val stale = decisions.keys.filter { !File(root, it).isFile }
                if (stale.isNotEmpty()) { stale.forEach { decisions.remove(it) }; save() }
            }
            return out
        }
    }

    private fun loadRecent(): List<String> = try {
        val a = JSONArray(prefs.getString("recent", "[]"))
        (0 until a.length()).map { a.getString(it) }.filter { File(it).isDirectory }
    } catch (_: Exception) { emptyList() }

    private fun addRecent(path: String) {
        val items = listOf(path) + loadRecent().filter { it != path }
        prefs.edit().putString("recent", JSONArray(items.take(10)).toString()).apply()
    }

    // -------------------------------------------------------------- utils --

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
        tmp.renameTo(out)
        return out
    }

    // ----------------------------------------------------------- previews --

    private fun thumb(f: File): File? {
        val k = kindOf(f)
        if (k == "image" && (f.length() < 1_500_000 || f.extension.lowercase() in setOf("svg", "gif"))) return f
        val out = cacheFile(f, "thumb", "jpg")
        if (out.exists()) return out
        return try {
            val bmp = when (k) {
                "image", "image-convert" -> ThumbnailUtils.createImageThumbnail(f, Size(480, 480), null)
                "video" -> ThumbnailUtils.createVideoThumbnail(f, Size(640, 640), null)
                "pdf", "doc" -> renderPdf(f, 1, 480)
                "audio" -> ThumbnailUtils.createAudioThumbnail(f, Size(480, 480), null)
                else -> null
            } ?: return null
            saveJpeg(bmp, out).also { bmp.recycle() }
        } catch (_: Exception) {
            if (k == "image") f else null
        }
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

        val s = session
        val open = setOf("/api/files", "/api/dirs", "/api/ping", "/api/root", "/api/pick", "/api/permission")
        if (path == "/api/permission") { act.openAllFilesSettings(); return ok() }
        if (!act.hasAllFilesAccess()) {
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
                val files = s.scan()
                return Response.json(JSONObject().put("root", s.root.absolutePath).put("name", s.root.name)
                    .put("recursive", s.recursive).put("truncated", s.truncated).put("files", files)
                    .put("version", VERSION).put("platform", "android").toString())
            }
            "/api/dirs" -> return Response.json(dirs(req.q("path")).toString())
            "/api/root" -> {
                val dir = File(body.optString("path", s?.root?.absolutePath ?: storageRoot.absolutePath))
                if (!dir.isDirectory) return Response.text(400, "not a directory")
                session = Session(dir.canonicalFile, body.optBoolean("recursive", true))
                return Response.json(JSONObject().put("ok", true).put("root", dir.absolutePath).toString())
            }
            "/api/pick" -> return Response.json("""{"error":"no-dialog"}""")
            "/api/decide" -> {
                val rel = body.optString("path")
                if (rel.isEmpty() || s!!.resolve(rel) == null) return Response.text(400, "bad path")
                synchronized(s) {
                    when (val d = body.optString("decision")) {
                        "keep", "delete", "favorite" -> s.decisions[rel] = d
                        else -> s.decisions.remove(rel)
                    }
                    s.save()
                }
                return ok()
            }
            "/api/reset" -> { synchronized(s!!) { s.decisions.clear(); s.save() }; return ok() }
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
            "/thumb" -> thumb(f)?.let { Response(200, if (it == f) mimeOf(f) else "image/jpeg", file = it) }
                ?: Response.text(404, "no thumbnail")
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
        val doomed = synchronized(s) { s.decisions.filter { it.value == "delete" }.keys.toList() }
            .mapNotNull { rel -> s.resolve(rel)?.takeIf { it.isFile }?.let { rel to it } }
        if (doomed.isNotEmpty()) {
            val sizes = doomed.associate { it.first to it.second.length() }
            val outcome = act.trashFiles(doomed.map { it.second })
            synchronized(s) {
                for ((rel, f) in doomed) {
                    val err = outcome[f]
                    if (err == null) { trashed.put(rel); freed += sizes[rel] ?: 0; s.decisions.remove(rel) }
                    else failed.put(JSONObject().put("path", rel).put("error", err))
                }
                s.save()
            }
        }
        val favDir = body.optString("favoritesFolder", "")
        if (favDir.isNotEmpty() && favDir != "null") {
            val dest = s.resolve(favDir)
            if (dest != null) {
                dest.mkdirs()
                synchronized(s) {
                    for ((rel, d) in s.decisions.toList()) {
                        if (d != "favorite") continue
                        val f = s.resolve(rel)?.takeIf { it.isFile && it.parentFile != dest } ?: continue
                        var target = File(dest, f.name); var i = 1
                        while (target.exists()) { target = File(dest, "${f.nameWithoutExtension} ($i).${f.extension}"); i++ }
                        if (f.renameTo(target)) {
                            act.rescan(listOf(f, target))
                            val newRel = target.relativeTo(s.root).path
                            s.decisions.remove(rel); s.decisions[newRel] = "favorite"; favorited.put(newRel)
                        }
                    }
                    s.save()
                }
            }
        }
        return JSONObject().put("trashed", trashed).put("failed", failed).put("favorited", favorited)
            .put("freed", freed).put("freedH", human(freed))
    }
}
