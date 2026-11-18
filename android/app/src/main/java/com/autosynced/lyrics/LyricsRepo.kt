package com.autosynced.lyrics

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChangedBy
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed class LyricsState {
    data object Idle : LyricsState()
    data object Loading : LyricsState()
    data class Found(val lyrics: Lyrics) : LyricsState()
    data object NotFound : LyricsState()
    data object NetworkError : LyricsState()
}

/** Fetches lyrics whenever the song changes (kept in memory for songs already played). */
object LyricsRepo {
    private val _state = MutableStateFlow<LyricsState>(LyricsState.Idle)
    val state: StateFlow<LyricsState> = _state
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val cache = HashMap<String, Lyrics?>()
    private var job: Job? = null
    private var started = false

    fun start(context: Context) {
        if (started) return
        started = true
        scope.launch {
            NowPlaying.state.distinctUntilChangedBy { it.track?.key }.collect { p ->
                val track = p.track
                job?.cancel()
                if (track == null) { _state.value = LyricsState.Idle; return@collect }
                if (cache.containsKey(track.key)) {
                    _state.value = cache[track.key]?.let { LyricsState.Found(it) } ?: LyricsState.NotFound
                    return@collect
                }
                _state.value = LyricsState.Loading
                job = launch {
                    val result = withContext(Dispatchers.IO) {
                        try {
                            Result.success(LyricsSources.fetch(track.title, track.artist, track.album, track.durationMs / 1000.0))
                        } catch (e: Exception) {
                            Result.failure(e)
                        }
                    }
                    if (NowPlaying.state.value.track?.key != track.key) return@launch
                    result.onSuccess {
                        cache[track.key] = it
                        _state.value = it?.let { l -> LyricsState.Found(l) } ?: LyricsState.NotFound
                    }.onFailure { _state.value = LyricsState.NetworkError }
                }
            }
        }
    }
}
