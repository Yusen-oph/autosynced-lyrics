package com.autosynced.lyrics

import android.service.notification.NotificationListenerService

/**
 * Android only lets an app read other apps' media sessions (what's playing, position) if it
 * has "notification access". This empty listener is what that permission is granted to; the
 * app never reads your notifications.
 */
class MediaListenerService : NotificationListenerService() {
    override fun onListenerConnected() {
        NowPlaying.start(applicationContext)
        LyricsRepo.start(applicationContext)
    }
}
