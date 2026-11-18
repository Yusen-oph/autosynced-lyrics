package com.autosynced.lyrics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** Runs on the PC (no phone needed): LRC parsing, matching, and real lookups. */
class LyricsTest {
    @Before fun useJdkBase64() {
        LyricsSources.base64Decode = { java.util.Base64.getMimeDecoder().decode(it) }
    }

    @Test fun parsesLrc() {
        val lines = LrcParser.parse("[ti:Song]\n[00:01.50]First line\n[00:03.00][00:10.00]Twice\n[00:05.20]\n[00:05.20]dup")
        // an empty line and a sung line on one time are both kept (same rule as the PC app)
        assertEquals(listOf(1500L, 3000L, 5200L, 5200L, 10000L), lines.map { it.timeMs })
        assertEquals("Twice", lines[1].text)
        val lyr = Lyrics(lines, true, "test")
        assertEquals(-1, lyr.indexAt(1000)); assertEquals(0, lyr.indexAt(2000)); assertEquals(4, lyr.indexAt(99999))
    }

    @Test fun keepsOriginalOverRomanisation() {
        val lines = LrcParser.parse("[00:01.00]夢ならばどれほど\n[00:01.00]yume naraba dore hodo")
        assertEquals(1, lines.size); assertEquals("夢ならばどれほど", lines[0].text)
    }

    @Test fun matchesTitles() {
        assertTrue(LyricsSources.similar("Shape of You", "Shape Of You (Remastered)") > 0.9)
        assertTrue(LyricsSources.similar("Viva La Vida", "Paradise") < 0.5)
    }

    @Test fun fetchesRealSongs() {
        val songs = listOf(Triple("Shape of You", "Ed Sheeran", 234.0), Triple("Viva La Vida", "Coldplay", 242.0),
            Triple("Lemon", "米津玄師", 255.0))
        for ((t, a, d) in songs) {
            val l = LyricsSources.fetch(t, a, "", d)
            println("$t — ${l?.source}, synced=${l?.synced}, ${l?.lines?.size} lines")
            assertTrue("$t should have synced lyrics", l != null && l.synced && l.lines.size > 20)
        }
        val k = LyricsSources.kugou("Shape of You", "Ed Sheeran", 234.0)
        println("Kugou fallback: ${k?.lines?.size} lines")
        assertTrue(k != null && k.lines.size > 20)
    }
}
