package io.github.laghab.photto

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.graphics.Color
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.provider.Settings
import android.util.Log
import android.view.WindowInsets
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import java.io.File
import java.security.SecureRandom
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class MainActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var server: MiniServer
    private lateinit var api: Api
    private var baseUrl = ""
    private var hadAccess = false
    @Volatile var lastPing = 0L

    private var trashLatch: CountDownLatch? = null
    @Volatile private var trashResult = RESULT_CANCELED

    private val debuggable get() = (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Test hooks (debug builds only): fixed port/token and a starting folder.
        val testToken = if (debuggable) intent.getStringExtra("photto.test.token") else null
        val testPort = if (debuggable) intent.getIntExtra("photto.test.port", 0) else 0
        val token = testToken ?: ByteArray(18).also { SecureRandom().nextBytes(it) }
            .joinToString("") { "%02x".format(it) }

        api = Api(this, token)
        server = MiniServer(testPort) { api.handle(it) }.also { it.start() }
        if (debuggable) {
            intent.getStringExtra("photto.test.root")?.let { root ->
                if (hasAllFilesAccess() && File(root).isDirectory) api.session = api.Session(File(root), true)
            }
            WebView.setWebContentsDebuggingEnabled(true)
            Log.i("photto", "server listening on 127.0.0.1:${server.port}")
        }
        baseUrl = "http://127.0.0.1:${server.port}/"

        web = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#12100E"))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.builtInZoomControls = false
            overScrollMode = WebView.OVER_SCROLL_NEVER
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url.toString()
                    if (url.startsWith(baseUrl)) return false
                    try { startActivity(Intent(Intent.ACTION_VIEW, request.url)) } catch (_: ActivityNotFoundException) {}
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                    if (debuggable) Log.d("photto-js", "${m.message()} (${m.sourceId()}:${m.lineNumber()})")
                    return true
                }
            }
            setOnApplyWindowInsetsListener { v, insets ->
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
                v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
        setContentView(web)
        hadAccess = hasAllFilesAccess()
        web.loadUrl("$baseUrl#t=$token")
    }

    override fun onResume() {
        super.onResume()
        val now = hasAllFilesAccess()
        if (now != hadAccess) {
            hadAccess = now
            web.evaluateJavascript("typeof load === 'function' && load()", null)
        }
    }

    @Deprecated("Still the simplest hook without AndroidX")
    override fun onBackPressed() {
        web.evaluateJavascript("window.phottoBack ? window.phottoBack() : false") { r ->
            if (r != "true") {
                @Suppress("DEPRECATION")
                super.onBackPressed()
            }
        }
    }

    override fun onDestroy() {
        server.stop()
        web.destroy()
        super.onDestroy()
    }

    // ------------------------------------------------- called by the API --

    fun hasAllFilesAccess() = Environment.isExternalStorageManager()

    fun openAllFilesSettings() = runOnUiThread {
        try {
            startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
        } catch (_: ActivityNotFoundException) {
            startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
        }
    }

    fun openExternally(f: File, mime: String): Boolean {
        val view = Intent(Intent.ACTION_VIEW).setDataAndType(FilesProvider.uriFor(f), mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        return try {
            runOnUiThread { startActivity(Intent.createChooser(view, "Open with").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            true
        } catch (_: ActivityNotFoundException) { false }
    }

    fun rescan(files: List<File>) {
        MediaScannerConnection.scanFile(this, files.map { it.absolutePath }.toTypedArray(), null, null)
    }

    /** Find (or create, by scanning) the MediaStore entry for a file. */
    private fun mediaUri(f: File): Uri? {
        val files = MediaStore.Files.getContentUri("external")
        fun query(): Uri? = contentResolver.query(files, arrayOf(MediaStore.MediaColumns._ID),
            "${MediaStore.MediaColumns.DATA}=?", arrayOf(f.absolutePath), null)?.use { c ->
            if (c.moveToFirst()) Uri.withAppendedPath(files, c.getLong(0).toString()) else null
        }
        query()?.let { return it }
        val latch = CountDownLatch(1)
        var scanned: Uri? = null
        MediaScannerConnection.scanFile(this, arrayOf(f.absolutePath), null) { _, uri -> scanned = uri; latch.countDown() }
        latch.await(15, TimeUnit.SECONDS)
        return query() ?: scanned
    }

    /**
     * Move files to Android's system trash (restorable for ~30 days from Files / Photos apps).
     * Android asks the user to confirm once per batch. Returns file -> error (null = trashed).
     */
    fun trashFiles(files: List<File>): Map<File, String?> {
        val result = HashMap<File, String?>()
        val uris = LinkedHashMap<File, Uri>()
        for (f in files) {
            val u = mediaUri(f)
            if (u == null) result[f] = "Android's media index doesn't know this file" else uris[f] = u
        }
        if (uris.isNotEmpty()) {
            val latch = CountDownLatch(1)
            trashLatch = latch
            trashResult = RESULT_CANCELED
            try {
                val pi = MediaStore.createTrashRequest(contentResolver, uris.values.toList(), true)
                runOnUiThread {
                    try {
                        startIntentSenderForResult(pi.intentSender, REQ_TRASH, null, 0, 0, 0)
                    } catch (e: Exception) {
                        Log.w("photto", "trash request failed", e); latch.countDown()
                    }
                }
                latch.await(10, TimeUnit.MINUTES)
            } catch (e: Exception) {
                Log.w("photto", "createTrashRequest failed", e)
                uris.keys.forEach { result[it] = e.message ?: "Android refused to trash this file" }
                return result
            }
            val okd = trashResult == RESULT_OK
            for (f in uris.keys) result[f] = if (!f.exists()) null else if (okd) "still there after trashing" else "cancelled"
        }
        return result
    }

    @Deprecated("Fine for a single dialog without AndroidX")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_TRASH) {
            trashResult = resultCode
            trashLatch?.countDown()
        }
    }

    companion object { private const val REQ_TRASH = 42 }
}
