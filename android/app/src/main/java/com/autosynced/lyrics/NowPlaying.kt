package com.autosynced.lyrics

import android.content.ComponentName
import android.content.Context
import android.graphics.Bitmap
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.core.app.NotificationManagerCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class Track(
    val title: String,
    val artist: String,
    val album: String,
    val durationMs: Long,
    val player: String,       // the music app's name, e.g. "Spotify"
) {
    val key get() = "$title\u0000$artist"
}

data class Playback(
    val track: Track? = null,
    val playing: Boolean = false,
    val positionMs: Long = 0,
    val updatedAt: Long = 0,  // SystemClock.elapsedRealtime() when positionMs was measured
    val speed: Float = 1f,
    val art: Bitmap? = null,
    val canSeek: Boolean = false,
) {
    /** Song position now, moving smoothly between updates from the music app. */
    fun position(now: Long = SystemClock.elapsedRealtime()): Long =
        if (playing) positionMs + ((now - updatedAt) * speed).toLong() else positionMs
}

/**
 * What any music app is playing, read from Android's media sessions. This needs
 * "notification access" for [MediaListenerService]; without it [hasAccess] is false.
 */
object NowPlaying {
    private val _state = MutableStateFlow(Playback())
    val state: StateFlow<Playback> = _state

    private var sessions: MediaSessionManager? = null
    private var controller: MediaController? = null
    private val main = Handler(Looper.getMainLooper())
    private lateinit var appContext: Context

    fun hasAccess(context: Context): Boolean =
        NotificationManagerCompat.getEnabledListenerPackages(context).contains(context.packageName)

    /** Start watching (safe to call more than once). */
    fun start(context: Context) {
        appContext = context.applicationContext
        if (sessions != null || !hasAccess(appContext)) return
        val msm = appContext.getSystemService(MediaSessionManager::class.java) ?: return
        val component = ComponentName(appContext, MediaListenerService::class.java)
        try {
            msm.addOnActiveSessionsChangedListener({ list -> choose(list.orEmpty()) }, component, main)
            sessions = msm
            choose(msm.getActiveSessions(component))
        } catch (_: SecurityException) {
            sessions = null                      // access was just revoked
        }
        main.post(poll)
    }

    // Some apps don't announce every change, so check now and then as well.
    private val poll = object : Runnable {
        override fun run() {
            val msm = sessions
            if (msm != null) {
                try {
                    choose(msm.getActiveSessions(ComponentName(appContext, MediaListenerService::class.java)))
                } catch (_: SecurityException) {}
            }
            main.postDelayed(this, 1000)
        }
    }

    private val callback = object : MediaController.Callback() {
        override fun onMetadataChanged(metadata: MediaMetadata?) = refresh()
        override fun onPlaybackStateChanged(state: PlaybackState?) = refresh()
        override fun onSessionDestroyed() { controller = null; _state.value = Playback() }
    }

    /** Follow the playing session (or the first one if none is playing). */
    private fun choose(list: List<MediaController>) {
        val playing = list.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING }
        val pick = playing ?: list.firstOrNull { it.packageName == controller?.packageName } ?: list.firstOrNull()
        if (pick?.sessionToken != controller?.sessionToken) {
            controller?.unregisterCallback(callback)
            controller = pick
            pick?.registerCallback(callback, main)
        }
        refresh()
    }

    private fun refresh() {
        val c = controller
        val meta = c?.metadata
        val title = meta?.getString(MediaMetadata.METADATA_KEY_TITLE)
        if (c == null || meta == null || title.isNullOrBlank()) {
            if (_state.value.track != null) _state.value = Playback()
            return
        }
        val artist = meta.getString(MediaMetadata.METADATA_KEY_ARTIST)
            ?: meta.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST) ?: ""
        val track = Track(title, artist, meta.getString(MediaMetadata.METADATA_KEY_ALBUM) ?: "",
            meta.getLong(MediaMetadata.METADATA_KEY_DURATION), playerName(c.packageName))
        val ps = c.playbackState
        val art = meta.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART)
            ?: meta.getBitmap(MediaMetadata.METADATA_KEY_ART)
            ?: meta.getBitmap(MediaMetadata.METADATA_KEY_DISPLAY_ICON)
        val new = Playback(
            track = track,
            playing = ps?.state == PlaybackState.STATE_PLAYING,
            positionMs = ps?.position ?: 0,
            updatedAt = ps?.lastPositionUpdateTime?.takeIf { it > 0 } ?: SystemClock.elapsedRealtime(),
            speed = ps?.playbackSpeed?.takeIf { it > 0f } ?: 1f,
            art = art ?: _state.value.art.takeIf { _state.value.track?.key == track.key },
            canSeek = ps != null && (ps.actions and PlaybackState.ACTION_SEEK_TO) != 0L,
        )
        if (new != _state.value) _state.value = new
    }

    private fun playerName(pkg: String): String = try {
        val pm = appContext.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (_: Exception) { pkg }

    // ---- controls ----------------------------------------------------------------------
    fun playPause() {
        val c = controller ?: return
        if (c.playbackState?.state == PlaybackState.STATE_PLAYING) c.transportControls.pause()
        else c.transportControls.play()
    }
    fun next() { controller?.transportControls?.skipToNext() }
    fun previous() { controller?.transportControls?.skipToPrevious() }
    fun seekTo(ms: Long) { controller?.transportControls?.seekTo(ms.coerceAtLeast(0)) }
}
