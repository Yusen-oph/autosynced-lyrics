package com.autosynced.lyrics.ui

import android.graphics.Bitmap
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.autosynced.lyrics.LyricsRepo
import com.autosynced.lyrics.LyricsState
import com.autosynced.lyrics.NowPlaying
import kotlinx.coroutines.delay

@Composable
fun AppScreen(
    hasAccess: Boolean,
    floatingOn: Boolean,
    style: String,
    onGrantAccess: () -> Unit,
    onToggleStyle: () -> Unit,
    onToggleFloating: () -> Unit,
) {
    val pal = LocalPalette.current
    val playback by NowPlaying.state.collectAsState()
    val tint = remember(playback.art) { dominantColor(playback.art) }
    val bgTop by animateColorAsState(
        if (pal.night) (tint ?: Color(0xFF3A3A3A)) else (tint?.copy(alpha = .22f) ?: Color(0x22FA2D48)),
        tween(600), label = "tint")
    Column(
        Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(bgTop, pal.bg), endY = 1400f))
            .background(if (pal.night) Color.Transparent else pal.bg.copy(alpha = .55f))
            .systemBarsPadding()
    ) {
        TopBar(style, floatingOn, onToggleStyle, onToggleFloating)
        if (!hasAccess) {
            AccessCard(onGrantAccess)
        } else {
            NowPlayingScreen(tint)
        }
    }
}

// ---- top bar --------------------------------------------------------------------------------
@Composable
private fun TopBar(style: String, floatingOn: Boolean, onToggleStyle: () -> Unit, onToggleFloating: () -> Unit) {
    val pal = LocalPalette.current
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("Autosynced Lyrics", color = pal.text, fontSize = 18.sp, fontWeight = FontWeight.Bold,
            modifier = Modifier.weight(1f))
        Pill(if (floatingOn) "Floating on" else "Floating off", floatingOn, onToggleFloating)
        Spacer(Modifier.width(8.dp))
        // Night / Day switch
        Box(
            Modifier.size(48.dp).clip(CircleShape).clickable(onClick = onToggleStyle),
            contentAlignment = Alignment.Center,
        ) {
            if (style == "day") SunIcon(pal.text) else MoonIcon(pal.text)
        }
    }
}

@Composable
private fun Pill(text: String, on: Boolean, onClick: () -> Unit) {
    val pal = LocalPalette.current
    Box(
        Modifier
            .height(36.dp)
            .clip(RoundedCornerShape(50))
            .background(if (on) pal.accent else pal.surface2)
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, color = if (on) pal.accentInk else pal.text, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
    }
}

// ---- first run: ask for notification access -------------------------------------------------
@Composable
private fun AccessCard(onGrantAccess: () -> Unit) {
    val pal = LocalPalette.current
    Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.Center) {
        Text("One step to start", color = pal.text, fontSize = 26.sp, fontWeight = FontWeight.ExtraBold)
        Spacer(Modifier.height(12.dp))
        Text("To see which song is playing in Spotify, YouTube Music, NetEase, QQ Music or any other " +
            "music app, Android needs you to turn on notification access for Autosynced Lyrics.\n\n" +
            "The app only reads what's playing; it never reads your notifications.",
            color = pal.sub, fontSize = 15.sp, lineHeight = 22.sp)
        Spacer(Modifier.height(24.dp))
        Box(
            Modifier.fillMaxWidth().height(52.dp).clip(RoundedCornerShape(50)).background(pal.accent)
                .clickable(onClick = onGrantAccess),
            contentAlignment = Alignment.Center,
        ) {
            Text("Turn on notification access", color = pal.accentInk, fontSize = 16.sp, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(12.dp))
        Text("In the list that opens, find Autosynced Lyrics, switch it on, then come back.",
            color = pal.faint, fontSize = 13.sp)
    }
}

// ---- now playing ----------------------------------------------------------------------------
@Composable
private fun ColumnScope.NowPlayingScreen(tint: Color?) {
    val pal = LocalPalette.current
    val playback by NowPlaying.state.collectAsState()
    val lyricsState by LyricsRepo.state.collectAsState()
    var now by remember { mutableLongStateOf(0L) }
    LaunchedEffect(Unit) {          // song position, smooth
        while (true) { now = NowPlaying.state.value.position(); delay(50) }
    }
    val track = playback.track

    // header: spinning record + title
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        SpinningRecord(playback.art, playback.playing, 72.dp)
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(track?.title ?: "Nothing playing", color = pal.text, fontSize = 20.sp, fontWeight = FontWeight.Bold,
                maxLines = 2, overflow = TextOverflow.Ellipsis)
            Text(track?.artist ?: "Play a song in any music app", color = if (pal.night) pal.sub else pal.accent,
                fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            if (track != null) Text("on ${track.player}", color = pal.faint, fontSize = 12.sp)
        }
    }

    // lyrics
    Box(
        Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)
            .clip(RoundedCornerShape(16.dp))
            .background(if (pal.night) (tint ?: Color(0xFF2A2A2A)).copy(alpha = .9f) else Color.Transparent)
    ) {
        when (val s = lyricsState) {
            is LyricsState.Found -> LyricsList(s.lyrics, now, playback.canSeek)
            is LyricsState.Loading -> Message("Loading lyrics…")
            is LyricsState.NotFound -> Message("No synced lyrics found for this song yet")
            is LyricsState.NetworkError -> Message("Network error, unable to fetch lyrics")
            is LyricsState.Idle -> Message("Play a song and its lyrics will appear here, line by line.")
        }
    }

    // progress + controls
    Controls(playback.track?.durationMs ?: 0L, now, playback.playing, playback.canSeek)
}

@Composable
private fun Message(text: String) {
    val pal = LocalPalette.current
    Box(Modifier.fillMaxSize().padding(28.dp), contentAlignment = Alignment.Center) {
        Text(text, color = if (pal.night) Color.White.copy(alpha = .8f) else pal.sub, fontSize = 20.sp,
            fontWeight = FontWeight.Bold, textAlign = TextAlign.Center, lineHeight = 28.sp)
    }
}

@Composable
private fun LyricsList(lyrics: com.autosynced.lyrics.Lyrics, now: Long, canSeek: Boolean) {
    val pal = LocalPalette.current
    val listState = rememberLazyListState()
    val current = if (lyrics.synced) lyrics.indexAt(now) else -1
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val third = with(LocalDensity.current) { (maxHeight * 0.32f).roundToPx() }
        LaunchedEffect(current) {       // keep the current line about a third of the way down
            if (current >= 0) listState.animateScrollToItem(current, -third)
        }
        LazyColumn(
            state = listState,
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 24.dp, bottom = maxHeight),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            itemsIndexed(lyrics.lines) { i, line ->
                val isNow = i == current
                val color = when {
                    !lyrics.synced -> pal.text
                    pal.night && isNow -> Color.White
                    pal.night && i < current -> Color.White.copy(alpha = .55f)
                    pal.night -> Color.White.copy(alpha = .38f)
                    isNow -> pal.text
                    else -> pal.text.copy(alpha = .3f)
                }
                val scale by animateFloatAsState(if (isNow) 1f else .96f, tween(300), label = "scale")
                Text(
                    line.text.ifBlank { "♪" },
                    color = color,
                    fontSize = (if (lyrics.synced) 24 else 18).sp * scale,
                    fontWeight = if (lyrics.synced) FontWeight.ExtraBold else FontWeight.SemiBold,
                    lineHeight = 30.sp,
                    modifier = Modifier.fillMaxWidth()
                        .clickable(enabled = lyrics.synced && canSeek) { NowPlaying.seekTo(line.timeMs) },
                )
            }
        }
    }
}

@Composable
private fun Controls(durationMs: Long, now: Long, playing: Boolean, canSeek: Boolean) {
    val pal = LocalPalette.current
    var dragging by remember { mutableStateOf(false) }
    var dragValue by remember { mutableFloatStateOf(0f) }
    val frac = if (durationMs > 0) (now.toFloat() / durationMs).coerceIn(0f, 1f) else 0f
    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(bottom = 10.dp)) {
        Slider(
            value = if (dragging) dragValue else frac,
            onValueChange = { dragging = true; dragValue = it },
            onValueChangeFinished = {
                if (canSeek && durationMs > 0) NowPlaying.seekTo((dragValue * durationMs).toLong())
                dragging = false
            },
            enabled = canSeek && durationMs > 0,
            colors = SliderDefaults.colors(thumbColor = pal.text, activeTrackColor = pal.text,
                inactiveTrackColor = pal.faint.copy(alpha = .4f), disabledThumbColor = pal.faint,
                disabledActiveTrackColor = pal.faint, disabledInactiveTrackColor = pal.faint.copy(alpha = .3f)),
        )
        Row(Modifier.fillMaxWidth()) {
            Text(fmt(if (dragging) (dragValue * durationMs).toLong() else now), color = pal.sub, fontSize = 12.sp)
            Spacer(Modifier.weight(1f))
            Text(fmt(durationMs), color = pal.sub, fontSize = 12.sp)
        }
        Row(Modifier.fillMaxWidth().padding(top = 4.dp), horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically) {
            RoundButton(56.dp, Color.Transparent, { NowPlaying.previous() }) { SkipIcon(pal.text, forward = false) }
            Spacer(Modifier.width(24.dp))
            RoundButton(68.dp, pal.accent, { NowPlaying.playPause() }) {
                if (playing) PauseIcon(pal.accentInk) else PlayIcon(pal.accentInk)
            }
            Spacer(Modifier.width(24.dp))
            RoundButton(56.dp, Color.Transparent, { NowPlaying.next() }) { SkipIcon(pal.text, forward = true) }
        }
    }
}

private fun fmt(ms: Long): String {
    val s = (ms / 1000).coerceAtLeast(0)
    return "%d:%02d".format(s / 60, s % 60)
}

@Composable
private fun RoundButton(size: androidx.compose.ui.unit.Dp, color: Color, onClick: () -> Unit, content: @Composable () -> Unit) {
    Box(Modifier.size(size).clip(CircleShape).background(color).clickable(onClick = onClick),
        contentAlignment = Alignment.Center) { content() }
}

// ---- spinning record ------------------------------------------------------------------------
@Composable
fun SpinningRecord(art: Bitmap?, playing: Boolean, size: androidx.compose.ui.unit.Dp) {
    var angle by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(playing) {
        var last = 0L
        while (playing) {
            withFrameNanos { t ->
                if (last != 0L) angle = (angle + (t - last) / 1e9f * 360f / 7f) % 360f   // one turn per 7 s
                last = t
            }
        }
    }
    Box(Modifier.size(size).rotate(angle), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val r = this.size.minDimension / 2
            drawCircle(Color(0xFF0D0D10), r)
            for (k in 1..7) drawCircle(Color(0x14FFFFFF), r * (0.95f - k * 0.07f), style = Stroke(1f))
            drawCircle(Color(0x33FFFFFF), r, style = Stroke(1.5f))
        }
        val labelMod = Modifier.size(size * 0.56f).clip(CircleShape)
        if (art != null) {
            Image(art.asImageBitmap(), contentDescription = null, contentScale = ContentScale.Crop, modifier = labelMod)
        } else {
            Box(labelMod.background(Color(0xFF3A3A3A)))
        }
        Box(Modifier.size(size * 0.06f).clip(CircleShape).background(Color(0xFF0A0A0A))
            .border(1.dp, Color(0x55FFFFFF), CircleShape))
    }
}

// ---- small drawn icons (play, pause, skip, sun, moon) ------------------------------------------
@Composable private fun PlayIcon(c: Color) = Canvas(Modifier.size(26.dp)) {
    val p = Path().apply { moveTo(size.width * .28f, size.height * .16f); lineTo(size.width * .86f, size.height * .5f)
        lineTo(size.width * .28f, size.height * .84f); close() }
    drawPath(p, c)
}
@Composable private fun PauseIcon(c: Color) = Canvas(Modifier.size(26.dp)) {
    val w = size.width * .22f
    drawRect(c, Offset(size.width * .22f, size.height * .16f), androidx.compose.ui.geometry.Size(w, size.height * .68f))
    drawRect(c, Offset(size.width * .56f, size.height * .16f), androidx.compose.ui.geometry.Size(w, size.height * .68f))
}
@Composable private fun SkipIcon(c: Color, forward: Boolean) = Canvas(Modifier.size(28.dp)) {
    val w = size.width; val h = size.height
    val p = Path()
    if (forward) { p.moveTo(w * .2f, h * .2f); p.lineTo(w * .68f, h * .5f); p.lineTo(w * .2f, h * .8f) }
    else { p.moveTo(w * .8f, h * .2f); p.lineTo(w * .32f, h * .5f); p.lineTo(w * .8f, h * .8f) }
    p.close(); drawPath(p, c)
    val bx = if (forward) w * .7f else w * .18f
    drawRect(c, Offset(bx, h * .2f), androidx.compose.ui.geometry.Size(w * .12f, h * .6f))
}
@Composable private fun MoonIcon(c: Color) = Canvas(Modifier.size(22.dp)
    .graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen)) {
    drawCircle(c, size.minDimension * .42f)
    drawCircle(Color.Black, size.minDimension * .34f, Offset(size.width * .66f, size.height * .36f),
        blendMode = androidx.compose.ui.graphics.BlendMode.Clear)
}
@Composable private fun SunIcon(c: Color) = Canvas(Modifier.size(22.dp)) {
    val ctr = center; val r = size.minDimension
    drawCircle(c, r * .2f)
    for (k in 0 until 8) {
        val a = Math.toRadians(k * 45.0)
        val s = Offset((ctr.x + Math.cos(a) * r * .32f).toFloat(), (ctr.y + Math.sin(a) * r * .32f).toFloat())
        val e = Offset((ctr.x + Math.cos(a) * r * .46f).toFloat(), (ctr.y + Math.sin(a) * r * .46f).toFloat())
        drawLine(c, s, e, strokeWidth = r * .08f)
    }
}
