package io.github.laghab.photto

import android.os.SystemClock
import android.util.Log
import org.json.JSONObject

/**
 * Debug-only timing and counter collection for the Android app.
 *
 * All methods are no-ops when [enabled] is false, so release builds pay nothing
 * and never expose the endpoint (the API checks the same flag).
 */
class Perf(private val enabled: Boolean, private val thumbs: ThumbPipeline?) {

    private val started = SystemClock.elapsedRealtime()
    private val lock = Any()
    private var firstPageMs = -1L
    private var scanStartMs = -1L
    private var firstIndexBatchMs = -1L
    private var indexCompleteMs = -1L
    private val client = JSONObject()

    fun markFirstPage() {
        if (!enabled) return
        synchronized(lock) {
            if (firstPageMs < 0) firstPageMs = SystemClock.elapsedRealtime() - started
        }
    }

    fun markScanStart() {
        if (!enabled) return
        synchronized(lock) {
            if (scanStartMs < 0) scanStartMs = SystemClock.elapsedRealtime() - started
        }
    }

    fun markFirstIndexBatch() {
        if (!enabled) return
        synchronized(lock) {
            if (firstIndexBatchMs < 0) firstIndexBatchMs = SystemClock.elapsedRealtime() - started
        }
    }

    fun markIndexComplete() {
        if (!enabled) return
        synchronized(lock) {
            if (indexCompleteMs < 0) indexCompleteMs = SystemClock.elapsedRealtime() - started
        }
    }

    fun acceptClient(values: JSONObject) {
        if (!enabled) return
        synchronized(lock) {
            for (key in listOf("firstPageMs", "firstCardMs", "swipeToNextMs", "reviewOpenMs",
                "reviewTiles", "reviewThumbs")) {
                if (values.has(key)) client.put(key, values.opt(key))
            }
        }
    }

    fun snapshot(): JSONObject {
        if (!enabled) return JSONObject().put("enabled", false)
        val out = JSONObject().put("enabled", true)
            .put("platform", "android")
            .put("uptimeMs", SystemClock.elapsedRealtime() - started)
        synchronized(lock) {
            out.put("firstPageMs", if (firstPageMs < 0) JSONObject.NULL else firstPageMs)
            out.put("scanStartMs", if (scanStartMs < 0) JSONObject.NULL else scanStartMs)
            out.put("firstIndexBatchMs", if (firstIndexBatchMs < 0) JSONObject.NULL else firstIndexBatchMs)
            out.put("indexCompleteMs", if (indexCompleteMs < 0) JSONObject.NULL else indexCompleteMs)
            val keys = client.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                out.put(k, client.opt(k))
            }
        }
        if (thumbs != null) out.put("thumbs", thumbs.stats())
        return out
    }

    fun logSummary() {
        if (!enabled) return
        Log.i("photto-perf", snapshot().toString())
    }
}
