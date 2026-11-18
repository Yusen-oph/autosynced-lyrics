package com.autosynced.lyrics

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import kotlin.math.abs

/**
 * The floating lyrics strip shown over other apps: current line + next line.
 * Drag to move · tap to open the app · long-press to turn it off.
 * Runs as a foreground service (with a small notification) so Android keeps it on screen.
 */
class FloatingLyricsService : Service() {
    companion object {
        private const val CHANNEL = "floating"
        private const val NOTIFICATION_ID = 7

        fun canDraw(c: Context) = Settings.canDrawOverlays(c)

        fun start(c: Context) {
            if (!canDraw(c)) return
            val i = Intent(c, FloatingLyricsService::class.java)
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
        }

        fun stop(c: Context) { c.stopService(Intent(c, FloatingLyricsService::class.java)) }
    }

    private lateinit var wm: WindowManager
    private var card: LinearLayout? = null
    private lateinit var current: TextView
    private lateinit var next: TextView
    private lateinit var params: WindowManager.LayoutParams
    private val handler = Handler(Looper.getMainLooper())
    private var shownStyle = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startAsForeground()
        wm = getSystemService(WINDOW_SERVICE) as WindowManager
        if (!canDraw(this)) { stopSelf(); return }
        NowPlaying.start(this)
        LyricsRepo.start(this)
        addOverlay()
        handler.post(tick)
    }

    override fun onDestroy() {
        handler.removeCallbacks(tick)
        card?.let { try { wm.removeView(it) } catch (_: Exception) {} }
        card = null
        super.onDestroy()
    }

    private fun dp(v: Float) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, resources.displayMetrics)

    private fun startAsForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Floating lyrics", NotificationManager.IMPORTANCE_MIN))
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n: Notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle("Floating lyrics is on")
            .setContentText("Tap to open Autosynced Lyrics. Long-press the lyrics to turn them off.")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun addOverlay() {
        val maxW = (resources.displayMetrics.widthPixels * 0.88f).toInt()
        current = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            typeface = Typeface.DEFAULT_BOLD
            maxWidth = maxW - dp(32f).toInt()
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
            gravity = Gravity.CENTER
        }
        next = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            maxWidth = maxW - dp(32f).toInt()
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            gravity = Gravity.CENTER
            setPadding(0, dp(3f).toInt(), 0, 0)
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(16f).toInt(), dp(10f).toInt(), dp(16f).toInt(), dp(11f).toInt())
            addView(current)
            addView(next)
            elevation = dp(6f)
        }
        card = box
        applyStyle()

        params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = Prefs.floatX(this@FloatingLyricsService)
            y = Prefs.floatY(this@FloatingLyricsService)
        }

        // drag to move, tap to open the app, long-press to turn off
        val slop = ViewConfiguration.get(this).scaledTouchSlop
        var downX = 0f; var downY = 0f; var startX = 0; var startY = 0; var moved = false
        val longPress = Runnable {
            Prefs.setFloating(this, false)
            Toast.makeText(this, "Floating lyrics turned off", Toast.LENGTH_SHORT).show()
            stopSelf()
        }
        box.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX; downY = e.rawY; startX = params.x; startY = params.y; moved = false
                    handler.postDelayed(longPress, ViewConfiguration.getLongPressTimeout().toLong() + 150)
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.rawX - downX; val dy = e.rawY - downY
                    if (!moved && (abs(dx) > slop || abs(dy) > slop)) {
                        moved = true; handler.removeCallbacks(longPress)
                    }
                    if (moved) {
                        params.x = startX + dx.toInt(); params.y = startY + dy.toInt()
                        try { wm.updateViewLayout(box, params) } catch (_: Exception) {}
                    }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    handler.removeCallbacks(longPress)
                    if (moved) Prefs.setFloatPos(this, params.x, params.y)
                    else if (e.actionMasked == MotionEvent.ACTION_UP) {
                        startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }
                }
            }
            true
        }
        wm.addView(box, params)
    }

    /** Night: near-black with white text · Day: frosted light with dark text. */
    private fun applyStyle() {
        val style = Prefs.style(this)
        if (style == shownStyle) return
        shownStyle = style
        val night = style != "day"
        card?.background = GradientDrawable().apply {
            cornerRadius = dp(18f)
            setColor(if (night) Color.parseColor("#EB181818") else Color.parseColor("#F2F7F7F9"))
            setStroke(dp(1f).toInt(), if (night) Color.parseColor("#26FFFFFF") else Color.parseColor("#1F000000"))
        }
        current.setTextColor(if (night) Color.WHITE else Color.parseColor("#1D1D1F"))
        next.setTextColor(if (night) Color.parseColor("#A7A7A7") else Color.parseColor("#6E6E73"))
    }

    private val tick = object : Runnable {
        override fun run() {
            update()
            handler.postDelayed(this, 100)
        }
    }

    private fun update() {
        val box = card ?: return
        applyStyle()
        val p = NowPlaying.state.value
        if (p.track == null) { box.visibility = View.GONE; return }
        box.visibility = View.VISIBLE
        var cur = "♪"; var nxt = ""
        when (val s = LyricsRepo.state.value) {
            is LyricsState.Loading -> { cur = p.track.title; nxt = "Loading lyrics…" }
            is LyricsState.NotFound -> { cur = p.track.title; nxt = "No synced lyrics for this song" }
            is LyricsState.NetworkError -> { cur = p.track.title; nxt = "Network error, unable to fetch lyrics" }
            is LyricsState.Found -> {
                val l = s.lyrics
                if (!l.synced) { cur = p.track.title; nxt = "These lyrics have no timing" }
                else {
                    val i = l.indexAt(p.position())
                    cur = if (i >= 0) l.lines[i].text.ifBlank { "♪" } else "♪"
                    nxt = l.lines.getOrNull(i + 1)?.text ?: ""
                }
            }
            else -> {}
        }
        if (current.text != cur) current.text = cur
        if (next.text != nxt) next.text = nxt
        next.visibility = if (nxt.isEmpty()) View.GONE else View.VISIBLE
    }
}
