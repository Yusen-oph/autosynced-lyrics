package com.autosynced.lyrics

import android.util.Base64 as AndroidBase64
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** One lyric line: when it starts (ms) and its words. */
data class Line(val timeMs: Long, val text: String)

data class Lyrics(
    val lines: List<Line>,
    val synced: Boolean,
    val source: String,
) {
    private val times = lines.map { it.timeMs }

    /** Index of the line showing at [positionMs] (-1 = before the first line). */
    fun indexAt(positionMs: Long): Int {
        var lo = 0
        var hi = times.size - 1
        var ans = -1
        while (lo <= hi) {
            val mid = (lo + hi) ushr 1
            if (times[mid] <= positionMs) { ans = mid; lo = mid + 1 } else hi = mid - 1
        }
        return ans
    }
}

/** Parses LRC text ("[01:23.45]words"), like the PC app's parse_lrc. */
object LrcParser {
    private val stamp = Regex("""\[(\d+):(\d+(?:\.\d+)?)]""")
    private val offsetTag = Regex("""^\[offset:\s*([+-]?\d+)\s*]""", setOf(RegexOption.IGNORE_CASE, RegexOption.MULTILINE))

    fun parse(text: String): List<Line> {
        val shift = offsetTag.find(text)?.groupValues?.get(1)?.toLongOrNull() ?: 0L
        val out = ArrayList<Line>()
        for (raw in text.lines()) {
            val stamps = stamp.findAll(raw).toList()
            if (stamps.isEmpty()) continue
            val words = stamp.replace(raw, "").trim()
            for (m in stamps) {
                val ms = (m.groupValues[1].toLong() * 60_000 + (m.groupValues[2].toDouble() * 1000).toLong()) - shift
                out.add(Line(max(0L, ms), words))
            }
        }
        out.sortBy { it.timeMs }
        // Two lines on one timestamp = the original plus a romanisation/translation: keep the original.
        val kept = ArrayList<Line>()
        for (l in out) {
            val last = kept.lastOrNull()
            if (last != null && abs(last.timeMs - l.timeMs) < 5 && last.text.isNotEmpty()) continue
            kept.add(l)
        }
        return kept
    }
}

/** Fetches lyrics from LRCLIB, then NetEase, then Kugou (like the PC app). */
object LyricsSources {
    private const val UA = "Mozilla/5.0 (Linux; Android 14) AutosyncedLyrics/0.1"
    private const val MAX_LENGTH_DIFF = 10.0      // seconds; more = probably another version
    private val credit = Regex("""(作词|作曲|编曲|制作人|监制|混音|母带|和声|演唱|原唱|填词|lyrics? by|written by|composed by|composer|lyricist|producer|produced by|arranged by|music by|words by)\s*[:：]""", RegexOption.IGNORE_CASE)
    private val noise = Regex("""\s*[-(\[].*?(remaster|live|version|edit|mix|feat|ft\.|mono|stereo).*$""", RegexOption.IGNORE_CASE)

    /** Base64 decoding (Android's on the phone; the JDK's in PC tests). */
    var base64Decode: (String) -> ByteArray = { AndroidBase64.decode(it, AndroidBase64.DEFAULT) }

    fun fetch(title: String, artist: String, album: String, durationSec: Double): Lyrics? {
        var plain: Lyrics? = null
        try {
            lrclib(title, artist, album, durationSec)?.let { if (it.synced) return it else plain = it }
        } catch (_: Exception) {}
        for (find in listOf(::netease, ::kugou)) {
            try { find(title, artist, durationSec)?.let { return it } } catch (_: Exception) {}
        }
        return plain
    }

    // ---- LRCLIB ---------------------------------------------------------------------
    fun lrclib(title: String, artist: String, album: String, durationSec: Double): Lyrics? {
        val exact = try {
            getJson("https://lrclib.net/api/get?" + query("track_name" to title, "artist_name" to artist,
                "album_name" to album, "duration" to Math.round(durationSec).toString())) as? JSONObject
        } catch (_: Exception) { null }
        exact?.optStringOrNull("syncedLyrics")?.let { return Lyrics(LrcParser.parse(it), true, "LRCLIB") }
        val clean = noise.replace(title, "").trim().ifEmpty { title }
        val results = getJson("https://lrclib.net/api/search?" + query("track_name" to clean, "artist_name" to artist)) as? JSONArray
        var best: JSONObject? = null
        var bestScore = Double.MAX_VALUE
        if (results != null) for (i in 0 until results.length()) {
            val r = results.getJSONObject(i)
            val synced = r.optStringOrNull("syncedLyrics") != null
            if (!synced && r.optStringOrNull("plainLyrics") == null) continue
            val diff = if (durationSec > 0) abs(r.optDouble("duration", 0.0) - durationSec) else 0.0
            if (durationSec > 0 && diff > 15) continue
            val score = (if (synced) 0.0 else 1000.0) + diff
            if (score < bestScore) { bestScore = score; best = r }
        }
        best?.optStringOrNull("syncedLyrics")?.let { return Lyrics(LrcParser.parse(it), true, "LRCLIB") }
        val plainText = best?.optStringOrNull("plainLyrics") ?: exact?.optStringOrNull("plainLyrics") ?: return null
        return Lyrics(plainText.lines().map { Line(0, it) }, false, "LRCLIB")
    }

    // ---- NetEase Cloud Music ------------------------------------------------------------
    fun netease(title: String, artist: String, durationSec: Double): Lyrics? {
        val headers = mapOf("Referer" to "https://music.163.com/")
        val res = getJson("https://music.163.com/api/search/get?" + query("s" to "$title $artist", "type" to "1",
            "limit" to "10", "offset" to "0"), headers) as? JSONObject ?: return null
        val songs = res.optJSONObject("result")?.optJSONArray("songs") ?: return null
        val candidates = (0 until songs.length()).map { i ->
            val s = songs.getJSONObject(i)
            val artists = s.optJSONArray("artists")
            val names = (0 until (artists?.length() ?: 0)).joinToString(" ") { artists!!.getJSONObject(it).optString("name") }
            Candidate(s.get("id").toString(), s.optString("name"), names, s.optDouble("duration", 0.0) / 1000)
        }
        for (c in pick(candidates, title, artist, durationSec)) {
            val lyr = getJson("https://music.163.com/api/song/lyric?" + query("id" to c.id, "lv" to "-1", "tv" to "-1"),
                headers) as? JSONObject ?: continue
            val lrc = lyr.optJSONObject("lrc")?.optStringOrNull("lyric") ?: continue
            usable(lrc, title, artist)?.let { return Lyrics(it, true, "NetEase") }
        }
        return null
    }

    // ---- Kugou ----------------------------------------------------------------------------
    fun kugou(title: String, artist: String, durationSec: Double): Lyrics? {
        val res = getJson("https://lyrics.kugou.com/search?" + query("ver" to "1", "man" to "yes", "client" to "pc",
            "keyword" to "$artist - $title", "duration" to (durationSec * 1000).toLong().toString())) as? JSONObject
            ?: return null
        val arr = res.optJSONArray("candidates") ?: return null
        val candidates = (0 until arr.length()).map { i ->
            val c = arr.getJSONObject(i)
            Candidate(c.get("id").toString() + "\u0000" + c.optString("accesskey"), c.optString("song"),
                c.optString("singer"), c.optDouble("duration", 0.0) / 1000)
        }
        for (c in pick(candidates, title, artist, durationSec)) {
            val (id, key) = c.id.split("\u0000")
            val d = getJson("https://lyrics.kugou.com/download?" + query("ver" to "1", "client" to "pc", "id" to id,
                "accesskey" to key, "fmt" to "lrc", "charset" to "utf8")) as? JSONObject ?: continue
            val content = d.optStringOrNull("content") ?: continue
            val lrc = String(base64Decode(content), Charsets.UTF_8)
            usable(lrc, title, artist)?.let { return Lyrics(it, true, "Kugou") }
        }
        return null
    }

    // ---- helpers ----------------------------------------------------------------------------
    data class Candidate(val id: String, val name: String, val artists: String, val seconds: Double)

    /** Best-matching few: similar title, similar artist (or same title + same length). */
    fun pick(songs: List<Candidate>, title: String, artist: String, durationSec: Double): List<Candidate> =
        songs.mapNotNull { s ->
            val lengthOk = durationSec <= 0 || s.seconds <= 0 || abs(s.seconds - durationSec) <= MAX_LENGTH_DIFF
            val t = similar(s.name, title)
            val a = similar(s.artists, artist)
            val sameLen = durationSec > 0 && s.seconds > 0 && abs(s.seconds - durationSec) <= 3
            if (lengthOk && t >= 0.6 && (a >= 0.5 || (t >= 0.9 && sameLen))) Pair(-(t + a), s) else null
        }.sortedBy { it.first }.take(3).map { it.second }

    /** Lyrics lines from LRC text with credit lines removed; null if almost empty. */
    fun usable(lrc: String, title: String, artist: String): List<Line>? {
        val header = norm(title + artist)
        val lines = LrcParser.parse(lrc).filterNot { l ->
            credit.containsMatchIn(l.text) || (l.text.isNotEmpty() && norm(l.text.replace(" - ", "")) in setOf(header, norm(artist + title)))
        }
        return if (lines.count { it.text.isNotBlank() } >= 3) lines else null
    }

    fun norm(s: String): String = java.text.Normalizer.normalize(s, java.text.Normalizer.Form.NFKC).lowercase()
        .let { noise.replace(it, "") }.replace(Regex("""[\W_]+"""), "")

    fun similar(a: String, b: String): Double {
        val x = norm(a); val y = norm(b)
        if (x.isEmpty() || y.isEmpty()) return 0.0
        if (x in y || y in x) return 1.0
        // 1 - edit distance / longer length
        val prev = IntArray(y.length + 1) { it }
        val cur = IntArray(y.length + 1)
        for (i in 1..x.length) {
            cur[0] = i
            for (j in 1..y.length) {
                cur[j] = min(min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + if (x[i - 1] == y[j - 1]) 0 else 1)
            }
            System.arraycopy(cur, 0, prev, 0, cur.size)
        }
        return 1.0 - prev[y.length].toDouble() / max(x.length, y.length)
    }

    private fun query(vararg pairs: Pair<String, String>) =
        pairs.joinToString("&") { (k, v) -> k + "=" + URLEncoder.encode(v, "UTF-8") }

    private fun getJson(url: String, headers: Map<String, String> = emptyMap()): Any? {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 6000
        conn.readTimeout = 8000
        conn.setRequestProperty("User-Agent", UA)
        headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
        try {
            if (conn.responseCode !in 200..299) return null
            val body = conn.inputStream.bufferedReader(Charsets.UTF_8).readText().trim()
            return when {
                body.startsWith("{") -> JSONObject(body)
                body.startsWith("[") -> JSONArray(body)
                else -> null
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) optString(key).takeIf { it.isNotBlank() } else null
}
