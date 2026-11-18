package com.autosynced.lyrics

import android.app.Application
import android.content.Context

class LyricsApp : Application() {
    override fun onCreate() {
        super.onCreate()
        NowPlaying.start(this)
        LyricsRepo.start(this)
    }
}

/** Small saved settings (style, floating lyrics on/off, where the floating bar sits). */
object Prefs {
    private fun p(c: Context) = c.getSharedPreferences("settings", Context.MODE_PRIVATE)
    fun style(c: Context): String = p(c).getString("style", "night") ?: "night"
    fun setStyle(c: Context, v: String) = p(c).edit().putString("style", v).apply()
    fun floating(c: Context): Boolean = p(c).getBoolean("floating", false)
    fun setFloating(c: Context, v: Boolean) = p(c).edit().putBoolean("floating", v).apply()
    fun floatX(c: Context): Int = p(c).getInt("float_x", 40)
    fun floatY(c: Context): Int = p(c).getInt("float_y", 300)
    fun setFloatPos(c: Context, x: Int, y: Int) = p(c).edit().putInt("float_x", x).putInt("float_y", y).apply()
}
