package io.github.laghab.photto

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.webkit.MimeTypeMap
import java.io.File
import java.io.FileNotFoundException

/** Read-only provider so other apps can open a file the user taps "Open" on (no AndroidX needed). */
class FilesProvider : ContentProvider() {
    companion object {
        private const val AUTHORITY = "io.github.laghab.photto.files"
        fun uriFor(f: File): Uri = Uri.Builder().scheme("content").authority(AUTHORITY).path(f.absolutePath).build()
    }

    private fun fileOf(uri: Uri): File {
        val f = File(uri.path ?: throw FileNotFoundException()).canonicalFile
        if (!f.isFile) throw FileNotFoundException(f.path)
        return f
    }

    override fun onCreate() = true

    override fun getType(uri: Uri): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(File(uri.path ?: "").extension.lowercase())
            ?: "application/octet-stream"

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor =
        ParcelFileDescriptor.open(fileOf(uri), ParcelFileDescriptor.MODE_READ_ONLY)

    override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?): Cursor {
        val f = fileOf(uri)
        val cols = projection ?: arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)
        return MatrixCursor(cols, 1).apply {
            addRow(cols.map { if (it == OpenableColumns.DISPLAY_NAME) f.name else if (it == OpenableColumns.SIZE) f.length() else null })
        }
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
}
