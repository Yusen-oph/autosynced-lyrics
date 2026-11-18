package com.autosynced.lyrics.ui

import android.graphics.Bitmap
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.core.graphics.ColorUtils

/** The two app styles, matching the PC app: Night (Spotify-like) and Day (Apple Music-like). */
data class Palette(
    val night: Boolean,
    val bg: Color,
    val surface: Color,
    val surface2: Color,
    val text: Color,
    val sub: Color,
    val faint: Color,
    val accent: Color,
    val accentInk: Color,
    val line: Color,
)

val NightPalette = Palette(
    night = true, bg = Color(0xFF121212), surface = Color(0xFF181818), surface2 = Color(0xFF282828),
    text = Color.White, sub = Color(0xFFB3B3B3), faint = Color(0xFF7A7A7A),
    accent = Color(0xFF1ED760), accentInk = Color.Black, line = Color(0xFF2C2C2C),
)

val DayPalette = Palette(
    night = false, bg = Color(0xFFF5F5F7), surface = Color.White, surface2 = Color(0xFFEDEDF0),
    text = Color(0xFF1D1D1F), sub = Color(0xFF6E6E73), faint = Color(0xFFA1A1A6),
    accent = Color(0xFFFA2D48), accentInk = Color.White, line = Color(0x14000000),
)

val LocalPalette = staticCompositionLocalOf { NightPalette }

/** The album cover's most striking colour (for the tinted lyrics panel), or null. */
fun dominantColor(bmp: Bitmap?): Color? {
    if (bmp == null) return null
    val small = try {
        val soft = if (bmp.config == Bitmap.Config.HARDWARE) bmp.copy(Bitmap.Config.ARGB_8888, false) else bmp
        Bitmap.createScaledBitmap(soft, 24, 24, true)
    } catch (_: Exception) { return null }
    val bins = DoubleArray(18); val hues = DoubleArray(18); val sats = DoubleArray(18)
    val hsl = FloatArray(3)
    for (y in 0 until 24) for (x in 0 until 24) {
        ColorUtils.colorToHSL(small.getPixel(x, y), hsl)
        val (h, s, l) = Triple(hsl[0], hsl[1], hsl[2])
        if (s < .25f || l < .15f || l > .85f) continue
        val k = ((h / 360f) * 18).toInt().coerceIn(0, 17)
        val w = s * (1 - kotlin.math.abs(l - .5f))
        bins[k] += w.toDouble(); hues[k] += (h * w).toDouble(); sats[k] += (s * w).toDouble()
    }
    val best = bins.indices.maxByOrNull { bins[it] } ?: return null
    if (bins[best] <= 0.0) return null
    val h = (hues[best] / bins[best]).toFloat()
    val s = (sats[best] / bins[best]).toFloat()
    return Color(ColorUtils.HSLToColor(floatArrayOf(h, minOf(.6f, s), .26f)))
}
