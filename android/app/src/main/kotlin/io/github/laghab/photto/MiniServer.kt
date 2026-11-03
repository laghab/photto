package io.github.laghab.photto

import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.io.RandomAccessFile
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.Executors

/** A tiny HTTP/1.1 server bound to 127.0.0.1, just enough for the photto web UI. */
class MiniServer(port: Int, private val handler: (Request) -> Response) {

    class Request(
        val method: String,
        val path: String,
        val query: Map<String, String>,
        val headers: Map<String, String>,
        val body: ByteArray,
    ) {
        fun q(name: String): String = query[name] ?: ""
    }

    class Response(
        val status: Int,
        val type: String,
        val bytes: ByteArray? = null,
        val file: File? = null,
        val stream: (() -> InputStream)? = null,
        val headers: Map<String, String> = emptyMap(),
    ) {
        companion object {
            fun json(text: String, status: Int = 200) =
                Response(status, "application/json", text.toByteArray(), headers = mapOf("Cache-Control" to "no-store"))
            fun text(status: Int, text: String) = Response(status, "text/plain; charset=utf-8", text.toByteArray())
        }
    }

    private val socket = ServerSocket(port, 64, InetAddress.getByName("127.0.0.1"))
    private val pool = Executors.newCachedThreadPool()
    val port: Int get() = socket.localPort

    fun start() {
        Thread({
            while (!socket.isClosed) {
                val client = try { socket.accept() } catch (e: IOException) { break }
                pool.execute { serve(client) }
            }
        }, "photto-http").apply { isDaemon = true }.start()
    }

    fun stop() {
        try { socket.close() } catch (_: IOException) {}
        pool.shutdownNow()
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b == -1) return if (buf.size() == 0) null else buf.toString("ISO-8859-1")
            if (b == '\n'.code) return buf.toString("ISO-8859-1").trimEnd('\r')
            buf.write(b)
            if (buf.size() > 16384) return null
        }
    }

    private fun parseQuery(q: String): Map<String, String> =
        q.split("&").filter { it.isNotEmpty() }.associate {
            val i = it.indexOf('=')
            if (i < 0) URLDecoder.decode(it, "UTF-8") to ""
            else URLDecoder.decode(it.substring(0, i), "UTF-8") to URLDecoder.decode(it.substring(i + 1), "UTF-8")
        }

    private fun serve(client: Socket) {
        client.use { s ->
            s.soTimeout = 60_000
            val input = BufferedInputStream(s.getInputStream())
            val out = s.getOutputStream()
            val line = readLine(input) ?: return
            val parts = line.split(" ")
            if (parts.size < 2) return
            val headers = HashMap<String, String>()
            while (true) {
                val h = readLine(input) ?: break
                if (h.isEmpty()) break
                val i = h.indexOf(':')
                if (i > 0) headers[h.substring(0, i).trim().lowercase()] = h.substring(i + 1).trim()
            }
            val len = headers["content-length"]?.toIntOrNull() ?: 0
            val body = ByteArray(len.coerceIn(0, 8 shl 20))
            var read = 0
            while (read < body.size) {
                val n = input.read(body, read, body.size - read)
                if (n < 0) break
                read += n
            }
            val target = parts[1]
            val qi = target.indexOf('?')
            val path = URLDecoder.decode(if (qi >= 0) target.substring(0, qi) else target, "UTF-8")
            val query = if (qi >= 0) parseQuery(target.substring(qi + 1)) else emptyMap()
            val req = Request(parts[0].uppercase(), path, query, headers, body)
            val resp = try { handler(req) } catch (e: Throwable) { Response.text(500, e.toString()) }
            try { write(out, req, resp) } catch (_: IOException) { /* client went away */ }
        }
    }

    private fun write(out: OutputStream, req: Request, resp: Response) {
        val head = StringBuilder()
        fun status(code: Int) = head.append("HTTP/1.1 $code ${reason(code)}\r\n")
        val extra = LinkedHashMap(resp.headers)
        when {
            resp.file != null -> {
                val f = resp.file
                val size = f.length()
                var start = 0L
                var end = size - 1
                val range = req.headers["range"]
                var partial = false
                if (range != null && range.startsWith("bytes=")) {
                    val spec = range.removePrefix("bytes=").split(",")[0].split("-")
                    try {
                        if (spec[0].isNotEmpty()) {
                            start = spec[0].toLong()
                            if (spec.size > 1 && spec[1].isNotEmpty()) end = spec[1].toLong()
                        } else if (spec.size > 1) {
                            start = maxOf(0, size - spec[1].toLong())
                        }
                        end = minOf(end, size - 1)
                        partial = true
                    } catch (_: NumberFormatException) {}
                }
                if (partial && (start > end || start >= size)) {
                    status(416); head.append("Content-Range: bytes */$size\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    out.write(head.toString().toByteArray()); return
                }
                val length = end - start + 1
                status(if (partial) 206 else 200)
                if (partial) extra["Content-Range"] = "bytes $start-$end/$size"
                extra["Accept-Ranges"] = "bytes"
                extra.putIfAbsent("Cache-Control", "private, max-age=300")
                head.append("Content-Type: ${resp.type}\r\nContent-Length: $length\r\n")
                extra.forEach { (k, v) -> head.append("$k: $v\r\n") }
                head.append("Connection: close\r\n\r\n")
                out.write(head.toString().toByteArray())
                if (req.method == "HEAD") return
                RandomAccessFile(f, "r").use { raf ->
                    raf.seek(start)
                    val buf = ByteArray(256 * 1024)
                    var left = length
                    while (left > 0) {
                        val n = raf.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                        if (n <= 0) break
                        out.write(buf, 0, n)
                        left -= n
                    }
                }
            }
            resp.stream != null -> {
                status(resp.status)
                head.append("Content-Type: ${resp.type}\r\n")
                extra.forEach { (k, v) -> head.append("$k: $v\r\n") }
                head.append("Connection: close\r\n\r\n")
                out.write(head.toString().toByteArray())
                resp.stream.invoke().use { it.copyTo(out, 256 * 1024) }
            }
            else -> {
                val bytes = resp.bytes ?: ByteArray(0)
                status(resp.status)
                head.append("Content-Type: ${resp.type}\r\nContent-Length: ${bytes.size}\r\n")
                extra.forEach { (k, v) -> head.append("$k: $v\r\n") }
                head.append("Connection: close\r\n\r\n")
                out.write(head.toString().toByteArray())
                if (req.method != "HEAD") out.write(bytes)
            }
        }
        out.flush()
    }

    private fun reason(code: Int) = when (code) {
        200 -> "OK"; 206 -> "Partial Content"; 400 -> "Bad Request"; 403 -> "Forbidden"; 404 -> "Not Found"
        409 -> "Conflict"; 415 -> "Unsupported Media Type"; 416 -> "Range Not Satisfiable"; else -> "Error"
    }
}
