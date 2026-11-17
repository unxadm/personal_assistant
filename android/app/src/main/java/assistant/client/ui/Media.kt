package assistant.client.ui

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageDecoder
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.math.max

private const val MAX_SIDE = 2048

/**
 * Read a picked photo as a JPEG no larger than 2048 px on its long side. Re-encoding keeps
 * uploads small and turns formats the server does not take (HEIC) into one it does.
 */
fun readAsJpeg(context: Context, uri: Uri): ByteArray {
    val bitmap = if (Build.VERSION.SDK_INT >= 28) {
        val source = ImageDecoder.createSource(context.contentResolver, uri)
        ImageDecoder.decodeBitmap(source) { decoder, info, _ ->
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
            val longest = max(info.size.width, info.size.height)
            if (longest > MAX_SIDE) {
                val scale = MAX_SIDE.toFloat() / longest
                decoder.setTargetSize(
                    (info.size.width * scale).toInt().coerceAtLeast(1),
                    (info.size.height * scale).toInt().coerceAtLeast(1),
                )
            }
        }
    } else {
        val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: throw java.io.IOException("cannot open the picture")
        decodeSampled(bytes, MAX_SIDE) ?: throw java.io.IOException("not a picture this phone can read")
    }
    val out = ByteArrayOutputStream()
    bitmap.compress(Bitmap.CompressFormat.JPEG, 85, out)
    bitmap.recycle()
    return out.toByteArray()
}

/** Decode at roughly [side] px, without loading the full-size picture first. */
fun decodeSampled(bytes: ByteArray, side: Int): Bitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    var sample = 1
    while (max(bounds.outWidth, bounds.outHeight) / (sample * 2) >= side) sample *= 2
    return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
}

/** Records a voice message to a temporary AAC/M4A file, which is deleted once read. */
class VoiceRecorder(private val context: Context) {
    private var recorder: MediaRecorder? = null
    private var file: File? = null

    val mime = "audio/mp4"

    fun start() {
        cancel()
        val target = File.createTempFile("voice", ".m4a", context.cacheDir)
        @Suppress("DEPRECATION")
        val next = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else MediaRecorder()
        try {
            next.setAudioSource(MediaRecorder.AudioSource.MIC)
            next.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            next.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            next.setAudioEncodingBitRate(64_000)
            next.setAudioSamplingRate(44_100)
            next.setOutputFile(target.absolutePath)
            next.prepare()
            next.start()
        } catch (e: Exception) {
            next.release()
            target.delete()
            throw e
        }
        recorder = next
        file = target
    }

    /** The recording, or null when nothing usable was captured (stopped at once). */
    fun stop(): ByteArray? {
        val active = recorder ?: return null
        val target = file
        recorder = null
        file = null
        return try {
            active.stop()
            target?.readBytes()?.takeIf { it.isNotEmpty() }
        } catch (e: RuntimeException) {
            null
        } finally {
            active.release()
            target?.delete()
        }
    }

    fun cancel() {
        stop()
    }
}
