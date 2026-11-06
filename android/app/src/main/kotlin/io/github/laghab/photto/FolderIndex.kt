package io.github.laghab.photto

import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import kotlin.math.max
import kotlin.math.min

/** A disposable SQLite cache of one folder's metadata; decisions stay in the state JSON. */
class FolderIndex(private val path: File) {

    companion object {
        const val SCHEMA_VERSION = 1
        const val MAX_PAGE_SIZE = 500
        const val DEFAULT_PAGE_SIZE = 200
        val SORTS = setOf("shuffle", "newest", "oldest", "largest", "name")
        val GROUPS = mapOf(
            "all" to null,
            "photos" to setOf("image", "image-convert"),
            "videos" to setOf("video"),
            "audio" to setOf("audio"),
            "docs" to setOf("pdf", "text", "doc"),
            "other" to setOf("archive", "other"),
        )
        private val DIGITS = Regex("\\d+")

        fun sortName(name: String): String = DIGITS.replace(name.lowercase()) { it.value.padStart(20, '0') }

        fun encodeCursor(sort: String, key: JSONArray): String {
            val raw = JSONObject().put("v", 1).put("s", sort).put("k", key)
                .toString().toByteArray(Charsets.UTF_8)
            return Base64.encodeToString(raw, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        }

        fun decodeCursor(cursor: String, sort: String): JSONArray? {
            if (cursor.isEmpty()) return null
            return try {
                val padded = cursor + "=".repeat((4 - cursor.length % 4) % 4)
                val raw = Base64.decode(padded, Base64.URL_SAFE)
                val o = JSONObject(String(raw, Charsets.UTF_8))
                val k = o.optJSONArray("k")
                if (o.optInt("v") != 1 || o.optString("s") != sort || k == null || k.length() != 2) null else k
            } catch (_: Exception) {
                null
            }
        }

        private const val SCHEMA = """
            CREATE TABLE IF NOT EXISTS files (
              rel       TEXT PRIMARY KEY,
              name      TEXT NOT NULL,
              dir       TEXT NOT NULL,
              ext       TEXT NOT NULL,
              kind      TEXT NOT NULL,
              size      INTEGER NOT NULL,
              mtime     REAL NOT NULL,
              sort_name TEXT NOT NULL,
              shuffle   REAL NOT NULL,
              decision  TEXT,
              seen      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS files_mtime   ON files(mtime, rel);
            CREATE INDEX IF NOT EXISTS files_size    ON files(size, rel);
            CREATE INDEX IF NOT EXISTS files_name    ON files(sort_name, rel);
            CREATE INDEX IF NOT EXISTS files_shuffle ON files(shuffle, rel);
            CREATE INDEX IF NOT EXISTS files_kind    ON files(kind, decision);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    }

    data class FileRow(
        val rel: String,
        val name: String,
        val dir: String,
        val ext: String,
        val kind: String,
        val size: Long,
        val mtime: Double,
        val sortName: String,
        val shuffle: Double,
        val seen: Int,
    )

    private val lock = Any()
    private var db: SQLiteDatabase = openWithRecovery()
    private var countsRevision = -1
    private var countsCache: JSONObject? = null

    // ---------------------------------------------------------- lifecycle --
    private fun open(): SQLiteDatabase {
        path.parentFile?.mkdirs()
        val database = SQLiteDatabase.openOrCreateDatabase(path, null)
        database.rawQuery("PRAGMA journal_mode=WAL", null).use { it.moveToFirst() }
        database.rawQuery("PRAGMA synchronous=NORMAL", null).use { it.moveToFirst() }
        database.rawQuery("PRAGMA busy_timeout=15000", null).use { it.moveToFirst() }
        return database
    }

    private fun openWithRecovery(): SQLiteDatabase {
        var database: SQLiteDatabase? = null
        return try {
            open().also { database = it }
                .let { initSchema(it) }
        } catch (_: Exception) {
            try { database?.close() } catch (_: Exception) {}
            for (suffix in listOf("", "-wal", "-shm")) {
                try { File(path.path + suffix).delete() } catch (_: Exception) {}
            }
            initSchema(open())
        }
    }

    private fun initSchema(database: SQLiteDatabase): SQLiteDatabase {
        // execSQL runs one statement; apply the schema statement by statement.
        SCHEMA.split(";").map { it.trim() }.filter { it.isNotEmpty() }
            .forEach { database.execSQL(it) }
        val version = meta(database, "schema_version")
        if (version != null && version.toIntOrNull() != SCHEMA_VERSION) {
            database.execSQL("DROP TABLE IF EXISTS files")
            database.execSQL("DROP TABLE IF EXISTS meta")
            SCHEMA.split(";").map { it.trim() }.filter { it.isNotEmpty() }
                .forEach { database.execSQL(it) }
        }
        if (version == null || version.toIntOrNull() != SCHEMA_VERSION) {
            database.execSQL("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                arrayOf("schema_version", SCHEMA_VERSION.toString()))
            for ((k, v) in listOf(
                "revision" to "0", "generation" to "0", "scan_state" to "idle",
                "scanned" to "0", "started_at" to "0", "error" to "", "truncated" to "0",
            )) {
                database.execSQL("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", arrayOf(k, v))
            }
        }
        return database
    }

    fun close() {
        synchronized(lock) {
            try { db.close() } catch (_: Exception) {}
        }
    }

    fun fileCount(): Int = synchronized(lock) {
        db.rawQuery("SELECT COUNT(*) FROM files", null).use { if (it.moveToFirst()) it.getInt(0) else 0 }
    }

    private fun meta(database: SQLiteDatabase, key: String): String? =
        database.rawQuery("SELECT value FROM meta WHERE key=?", arrayOf(key)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }

    private fun meta(key: String): String? = meta(db, key)

    private fun setMeta(vararg pairs: Pair<String, Any>) {
        for ((key, value) in pairs) {
            db.execSQL("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", arrayOf(key, value.toString()))
        }
    }

    fun revision(): Int = synchronized(lock) { meta("revision")?.toIntOrNull() ?: 0 }

    fun currentGeneration(): Int = synchronized(lock) { meta("generation")?.toIntOrNull() ?: 0 }

    fun truncated(): Boolean = synchronized(lock) { meta("truncated")?.toIntOrNull() == 1 }

    fun scanInfo(): JSONObject = synchronized(lock) {
        JSONObject()
            .put("state", meta("scan_state") ?: "idle")
            .put("scanned", meta("scanned")?.toIntOrNull() ?: 0)
            .put("startedAt", meta("started_at")?.toLongOrNull() ?: 0L)
            .put("error", (meta("error") ?: "").ifEmpty { JSONObject.NULL })
    }

    // ----------------------------------------------------------- scanning --
    fun beginScan(): Int = synchronized(lock) {
        val generation = currentGeneration() + 1
        db.beginTransaction()
        try {
            setMeta(
                "generation" to generation, "scan_state" to "scanning", "scanned" to 0,
                "started_at" to System.currentTimeMillis() / 1000, "error" to "", "truncated" to 0,
            )
            bumpRevision()
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        generation
    }

    fun upsertBatch(rows: List<FileRow>, generation: Int, scanned: Int,
                    decisions: Map<String, String> = emptyMap()): Boolean = synchronized(lock) {
        if (currentGeneration() != generation) return false
        db.beginTransaction()
        try {
            val statement = db.compileStatement(
                """INSERT INTO files(rel,name,dir,ext,kind,size,mtime,sort_name,shuffle,decision,seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(rel) DO UPDATE SET name=excluded.name, dir=excluded.dir,
                   ext=excluded.ext, kind=excluded.kind, size=excluded.size, mtime=excluded.mtime,
                   sort_name=excluded.sort_name, seen=excluded.seen""")
            for (r in rows) {
                statement.clearBindings()
                statement.bindString(1, r.rel)
                statement.bindString(2, r.name)
                statement.bindString(3, r.dir)
                statement.bindString(4, r.ext)
                statement.bindString(5, r.kind)
                statement.bindLong(6, r.size)
                statement.bindDouble(7, r.mtime)
                statement.bindString(8, r.sortName)
                statement.bindDouble(9, r.shuffle)
                val decision = decisions[r.rel]
                if (decision == null) statement.bindNull(10) else statement.bindString(10, decision)
                statement.bindLong(11, r.seen.toLong())
                statement.executeInsert()
            }
            setMeta("scanned" to scanned)
            bumpRevision()
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        true
    }

    /** Delete rows the finished generation did not see; return the removed relative paths. */
    fun finishScan(generation: Int, scanned: Int, truncated: Boolean): List<String> = synchronized(lock) {
        if (currentGeneration() != generation) return emptyList()
        val gone = ArrayList<String>()
        db.beginTransaction()
        try {
            db.rawQuery("SELECT rel FROM files WHERE seen != ?", arrayOf(generation.toString())).use { c ->
                while (c.moveToNext()) gone.add(c.getString(0))
            }
            db.execSQL("DELETE FROM files WHERE seen != ?", arrayOf(generation.toString()))
            setMeta("scan_state" to "complete", "scanned" to scanned,
                "truncated" to if (truncated) 1 else 0, "error" to "")
            bumpRevision()
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        gone
    }

    fun failScan(generation: Int, error: String, scanned: Int) {
        synchronized(lock) {
            if (currentGeneration() != generation) return
            db.beginTransaction()
            try {
                setMeta("scan_state" to "error", "error" to error, "scanned" to scanned)
                bumpRevision()
                db.setTransactionSuccessful()
            } finally {
                db.endTransaction()
            }
        }
    }

    fun setScanState(state: String, error: String) {
        synchronized(lock) {
            db.beginTransaction()
            try {
                setMeta("scan_state" to state, "error" to error)
                bumpRevision()
                db.setTransactionSuccessful()
            } finally {
                db.endTransaction()
            }
        }
    }

    fun setScanError(error: String) = setScanState("error", error)

    private fun bumpRevision() {
        setMeta("revision" to ((meta("revision")?.toIntOrNull() ?: 0) + 1))
    }

    // ---------------------------------------------------------- decisions --
    fun applyDecisions(decisions: Map<String, String>) {
        synchronized(lock) {
            db.beginTransaction()
            try {
                db.execSQL("UPDATE files SET decision=NULL")
                val statement = db.compileStatement("UPDATE files SET decision=? WHERE rel=?")
                for ((rel, decision) in decisions) {
                    statement.clearBindings()
                    statement.bindString(1, decision)
                    statement.bindString(2, rel)
                    statement.executeUpdateDelete()
                }
                db.setTransactionSuccessful()
            } finally {
                db.endTransaction()
            }
            countsRevision = -1
        }
    }

    fun setDecision(rel: String, decision: String?) {
        synchronized(lock) {
            if (decision == null) db.execSQL("UPDATE files SET decision=NULL WHERE rel=?", arrayOf(rel))
            else db.execSQL("UPDATE files SET decision=? WHERE rel=?", arrayOf(decision, rel))
            countsRevision = -1
        }
    }

    fun removeRows(rels: Collection<String>) {
        if (rels.isEmpty()) return
        synchronized(lock) {
            db.beginTransaction()
            try {
                val statement = db.compileStatement("DELETE FROM files WHERE rel=?")
                for (rel in rels) {
                    statement.clearBindings()
                    statement.bindString(1, rel)
                    statement.executeUpdateDelete()
                }
                db.setTransactionSuccessful()
            } finally {
                db.endTransaction()
            }
            countsRevision = -1
        }
    }

    // ------------------------------------------------------------ queries --
    fun counts(): JSONObject = synchronized(lock) {
        val revision = revision()
        if (revision == countsRevision && countsCache != null) return JSONObject(countsCache.toString())
        val result = JSONObject()
        var total = 0; var undecided = 0; var keep = 0; var delete = 0; var favorite = 0
        var photos = 0; var videos = 0; var audio = 0; var docs = 0; var other = 0
        var photosLeft = 0; var videosLeft = 0; var audioLeft = 0; var docsLeft = 0; var otherLeft = 0
        var keepBytes = 0L; var deleteBytes = 0L; var favBytes = 0L
        db.rawQuery(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN decision IS NULL THEN 1 ELSE 0 END) undecided,
                      SUM(CASE WHEN decision='keep' THEN 1 ELSE 0 END) kept,
                      SUM(CASE WHEN decision='delete' THEN 1 ELSE 0 END) deleted,
                      SUM(CASE WHEN decision='favorite' THEN 1 ELSE 0 END) favorited,
                      SUM(CASE WHEN kind IN ('image','image-convert') THEN 1 ELSE 0 END) photos,
                      SUM(CASE WHEN kind='video' THEN 1 ELSE 0 END) videos,
                      SUM(CASE WHEN kind='audio' THEN 1 ELSE 0 END) audio,
                      SUM(CASE WHEN kind IN ('pdf','text','doc') THEN 1 ELSE 0 END) docs,
                      SUM(CASE WHEN kind IN ('archive','other') THEN 1 ELSE 0 END) other,
                      SUM(CASE WHEN kind IN ('image','image-convert') AND decision IS NULL THEN 1 ELSE 0 END) photos_left,
                      SUM(CASE WHEN kind='video' AND decision IS NULL THEN 1 ELSE 0 END) videos_left,
                      SUM(CASE WHEN kind='audio' AND decision IS NULL THEN 1 ELSE 0 END) audio_left,
                      SUM(CASE WHEN kind IN ('pdf','text','doc') AND decision IS NULL THEN 1 ELSE 0 END) docs_left,
                      SUM(CASE WHEN kind IN ('archive','other') AND decision IS NULL THEN 1 ELSE 0 END) other_left,
                      SUM(CASE WHEN decision='keep' THEN size ELSE 0 END) keep_bytes,
                      SUM(CASE WHEN decision='delete' THEN size ELSE 0 END) delete_bytes,
                      SUM(CASE WHEN decision='favorite' THEN size ELSE 0 END) fav_bytes
               FROM files""", null
        ).use { c ->
            if (c.moveToFirst()) {
                total = c.getInt(0); undecided = c.getInt(1); keep = c.getInt(2)
                delete = c.getInt(3); favorite = c.getInt(4); photos = c.getInt(5)
                videos = c.getInt(6); audio = c.getInt(7); docs = c.getInt(8); other = c.getInt(9)
                photosLeft = c.getInt(10); videosLeft = c.getInt(11); audioLeft = c.getInt(12)
                docsLeft = c.getInt(13); otherLeft = c.getInt(14)
                keepBytes = c.getLong(15); deleteBytes = c.getLong(16); favBytes = c.getLong(17)
            }
        }
        result.put("total", total).put("undecided", undecided)
            .put("byDecision", JSONObject().put("keep", keep).put("delete", delete).put("favorite", favorite))
            .put("byGroup", JSONObject().put("photos", photos).put("videos", videos)
                .put("audio", audio).put("docs", docs).put("other", other))
            .put("undecidedByGroup", JSONObject().put("photos", photosLeft).put("videos", videosLeft)
                .put("audio", audioLeft).put("docs", docsLeft).put("other", otherLeft))
            .put("bytesByDecision", JSONObject().put("keep", keepBytes).put("delete", deleteBytes)
                .put("favorite", favBytes))
        countsRevision = revision
        countsCache = result
        JSONObject(result.toString())
    }

    fun page(
        cursor: String = "", offset: Int = 0, limit: Int = DEFAULT_PAGE_SIZE,
        sort: String = "shuffle", group: String = "all", decision: String = "none",
    ): JSONObject = synchronized(lock) {
        val order = if (sort in SORTS) sort else "shuffle"
        val kinds = GROUPS[group] ?: GROUPS["all"]
        val pageLimit = max(1, min(limit, MAX_PAGE_SIZE))
        val pageOffset = max(0, offset)
        val where = ArrayList<String>()
        val args = ArrayList<String>()

        if (kinds != null) {
            where.add("kind IN (${kinds.joinToString(",") { "?" }})")
            args.addAll(kinds)
        }
        when (decision) {
            "none" -> where.add("decision IS NULL")
            "keep", "delete", "favorite" -> {
                where.add("decision=?")
                args.add(decision)
            }
        }

        val key = decodeCursor(cursor, order)
        if (key != null) {
            when (order) {
                "shuffle" -> where.add("(shuffle > ? OR (shuffle = ? AND rel > ?))")
                "newest" -> where.add("(mtime < ? OR (mtime = ? AND rel > ?))")
                "oldest" -> where.add("(mtime > ? OR (mtime = ? AND rel > ?))")
                "largest" -> where.add("(size < ? OR (size = ? AND rel > ?))")
                else -> where.add("(sort_name > ? OR (sort_name = ? AND rel > ?))")
            }
            val first = if (order == "largest") key.getLong(0).toString()
                else if (order == "name") key.getString(0) else key.getDouble(0).toString()
            val second = key.getString(1)
            args.add(first); args.add(first); args.add(second)
        }

        val clause = if (where.isEmpty()) "" else " WHERE " + where.joinToString(" AND ")
        val orderBy = when (order) {
            "shuffle" -> "shuffle ASC, rel ASC"
            "newest" -> "mtime DESC, rel ASC"
            "oldest" -> "mtime ASC, rel ASC"
            "largest" -> "size DESC, rel ASC"
            else -> "sort_name ASC, rel ASC"
        }
        val limitSql = if (key != null) " LIMIT ?" else " LIMIT ? OFFSET ?"
        val queryArgs = ArrayList(args).apply {
            add((pageLimit + 1).toString())
            if (key == null) add(pageOffset.toString())
        }

        val files = JSONArray()
        var lastKey: JSONArray? = null
        var count = 0
        db.rawQuery("SELECT * FROM files$clause ORDER BY $orderBy$limitSql", queryArgs.toTypedArray()).use { c ->
            while (c.moveToNext()) {
                if (count < pageLimit) files.put(cursorFile(c))
                if (count == pageLimit - 1) lastKey = cursorKey(c, order)
                count++
            }
        }
        val next = if (count > pageLimit) encodeCursor(order, lastKey!!) else null
        JSONObject()
            .put("offset", pageOffset)
            .put("limit", pageLimit)
            .put("effectiveLimit", pageLimit)
            .put("files", files)
            .put("nextCursor", next ?: JSONObject.NULL)
    }

    private fun cursorFile(c: Cursor): JSONObject {
        val size = c.getLong(c.getColumnIndexOrThrow("size"))
        return JSONObject()
            .put("path", c.getString(c.getColumnIndexOrThrow("rel")))
            .put("name", c.getString(c.getColumnIndexOrThrow("name")))
            .put("dir", c.getString(c.getColumnIndexOrThrow("dir")))
            .put("ext", c.getString(c.getColumnIndexOrThrow("ext")))
            .put("kind", c.getString(c.getColumnIndexOrThrow("kind")))
            .put("size", size)
            .put("sizeH", human(size))
            .put("mtime", c.getDouble(c.getColumnIndexOrThrow("mtime")))
            .put("decision", c.getString(c.getColumnIndexOrThrow("decision")) ?: JSONObject.NULL)
    }

    private fun cursorKey(c: Cursor, sort: String): JSONArray = when (sort) {
        "shuffle" -> JSONArray().put(c.getDouble(c.getColumnIndexOrThrow("shuffle")))
            .put(c.getString(c.getColumnIndexOrThrow("rel")))
        "newest", "oldest" -> JSONArray().put(c.getDouble(c.getColumnIndexOrThrow("mtime")))
            .put(c.getString(c.getColumnIndexOrThrow("rel")))
        "largest" -> JSONArray().put(c.getLong(c.getColumnIndexOrThrow("size")))
            .put(c.getString(c.getColumnIndexOrThrow("rel")))
        else -> JSONArray().put(c.getString(c.getColumnIndexOrThrow("sort_name")))
            .put(c.getString(c.getColumnIndexOrThrow("rel")))
    }

    private fun human(n: Long): String {
        var v = n.toDouble(); var i = 0
        val units = arrayOf("B", "KB", "MB", "GB", "TB")
        while (v >= 1024 && i < units.size - 1) { v /= 1024; i++ }
        return if (i == 0) "$n B" else "%.1f %s".format(v, units[i])
    }
}
