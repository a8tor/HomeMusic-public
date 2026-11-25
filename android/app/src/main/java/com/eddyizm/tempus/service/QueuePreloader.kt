package com.eddyizm.tempus.service

import android.content.Context
import android.net.ConnectivityManager
import android.net.Uri
import android.os.Handler
import android.os.SystemClock
import android.util.Log
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.Timeline
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.cache.CacheWriter
import androidx.media3.datasource.cache.ContentMetadata
import com.eddyizm.tempus.homemusic.CacheLength
import com.eddyizm.tempus.homemusic.HomeTraffic
import com.eddyizm.tempus.homemusic.HomeTrafficStats
import com.eddyizm.tempus.homemusic.HubLight
import com.eddyizm.tempus.homemusic.HubTracks
import com.eddyizm.tempus.homemusic.HubWave
import com.eddyizm.tempus.homemusic.QueueLoading
import com.eddyizm.tempus.util.Constants
import com.eddyizm.tempus.util.DownloadUtil
import com.eddyizm.tempus.util.Preferences
import com.eddyizm.tempus.util.StreamingCacheKeyFactory
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Pre-caches the next tracks of the play queue into the streaming cache, so
 * skips start instantly and playback survives short connectivity drops. The
 * player reads through the same cache with the same keys, so anything written
 * here is picked up transparently.
 *
 * Runs only when the user enabled it (settings > data), never for radio
 * streams or already-downloaded tracks, and by default only on unmetered
 * networks.
 *
 * HomeMusic: only the first seconds of each next track (`QueueLoading`), after five
 * seconds of playback and, in a wave, after the Hub answers for the current track.
 * Traffic economy caps the batch at one head. A transcoded stream is skipped:
 * the player could not continue its head with a Range request.
 */
@UnstableApi
object QueuePreloader {
    private const val TAG = "QueuePreloader"
    private const val STABLE_PRELOAD_MS = 5_000L

    private val executor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, HomeTrafficStats.PRELOAD_THREAD).apply { priority = Thread.MIN_PRIORITY }
    }
    private val generation = AtomicInteger(0)

    @Volatile
    private var activeWriter: CacheWriter? = null

    /** Target of the task currently queued/executing; empty when idle. */
    @Volatile
    private var runningUris: List<Head> = emptyList()

    /** Keys of heads stored without the file's length that were dropped to be loaded again (once per key). */
    private val relearned = HashSet<String>()

    // Only accessed on the player's application thread.
    private var playback: PlaybackGate? = null

    private class PlaybackGate(val context: Context, val player: Player) : Player.Listener {
        private val handler = Handler(player.applicationLooper)
        private val retry = Runnable { preload(context, player) }
        private val window = Timeline.Window()
        private var currentUid = currentWindowUid()
        private var playedMs = 0L
        private var startedAt = SystemClock.elapsedRealtime().takeIf { player.isPlaying }

        private fun currentWindowUid(): Any? {
            val timeline = player.currentTimeline
            val index = player.currentMediaItemIndex
            return if (index in 0 until timeline.windowCount) timeline.getWindow(index, window).uid else null
        }

        fun checkTransition(repeated: Boolean = false) {
            val uid = currentWindowUid()
            if (!repeated && uid == currentUid) return
            currentUid = uid
            invalidateBatch()
            playedMs = 0L
            startedAt = SystemClock.elapsedRealtime().takeIf { player.isPlaying }
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            checkTransition(repeated = reason == Player.MEDIA_ITEM_TRANSITION_REASON_REPEAT)
            preload(context, player)
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            val now = SystemClock.elapsedRealtime()
            startedAt?.let { playedMs += now - it }
            startedAt = now.takeIf { isPlaying }
            preload(context, player)
        }

        fun ready(): Boolean {
            handler.removeCallbacks(retry)
            if (!player.isPlaying) return false
            // A seek (including resume from a saved position) is not time spent playing.
            val played = playedMs + (startedAt?.let { SystemClock.elapsedRealtime() - it } ?: 0L)
            val remaining = maxOf(STABLE_PRELOAD_MS - played, STABLE_PRELOAD_MS - player.currentPosition)
            if (remaining <= 0L) return true
            handler.postDelayed(retry, remaining.coerceAtLeast(100L))
            return false
        }

        fun detach() {
            handler.removeCallbacks(retry)
            player.removeListener(this)
        }
    }

    /** Must be called from the player's application thread. */
    fun preload(context: Context, player: Player) {
        val gate = playback?.takeIf { it.player === player } ?: run {
            cancel()
            PlaybackGate(context.applicationContext, player).also {
                playback = it
                player.addListener(it)
            }
        }
        // The service can call us from onTimelineChanged before our transition callback.
        gate.checkTransition()
        val economy = HomeTraffic.economy()
        val count = HomeTraffic.precacheCount(Preferences.getPrecacheTracksCount(), economy)
        val appContext = gate.context
        if (count <= 0 || Preferences.getStreamingCacheSize() <= 0L ||
            (Preferences.isPrecacheWifiOnly() && isMetered(appContext))) {
            invalidateBatch()
            return
        }

        val ready = gate.ready() && !player.isLoading && HubWave.preloadReady(player)
        if (!ready && runningUris.isEmpty()) return
        val uris = collectUpcomingStreamUris(appContext, player, count, economy)
        // Reconcile an existing batch even while the player is loading (e.g. after a delta).
        if (!ready) {
            synchronized(this) {
                if (!uris.containsAll(runningUris)) invalidateBatch()
            }
            return
        }

        val myGeneration: Int
        synchronized(this) {
            // The in-flight task is already working towards exactly this target:
            // leave it alone. preload() fires on every timeline/metadata/network
            // event, and restarting the writer each time discards all progress on
            // unknown-length (transcoded) content, so the precache never finishes.
            if (uris == runningUris) return

            myGeneration = generation.incrementAndGet()
            runningUris = uris
            activeWriter?.cancel()
        }

        if (uris.isEmpty()) return

        executor.execute {
            for (head in uris) {
                if (generation.get() != myGeneration) return@execute
                if (isCached(appContext, head)) continue
                cacheTrack(appContext, head, myGeneration)
            }
            // Clear the target so the next preload() retries tracks that failed
            // (fully cached ones are skipped by the check above anyway).
            synchronized(this) {
                if (generation.get() == myGeneration) runningUris = emptyList()
            }
        }
    }

    /** Stop the worker and release the player listener on service teardown. */
    fun cancel() {
        playback?.detach()
        playback = null
        invalidateBatch()
    }

    private fun invalidateBatch() {
        synchronized(this) {
            generation.incrementAndGet()
            runningUris = emptyList()
            activeWriter?.cancel()
        }
    }

    /** A next track to pre-cache: its first [bytes]. */
    private data class Head(val uri: Uri, val bytes: Long)

    private fun collectUpcomingStreamUris(context: Context, player: Player, count: Int, economy: Boolean): List<Head> {
        val timeline = player.currentTimeline
        if (timeline.isEmpty) return emptyList()

        val currentIndex = player.currentMediaItemIndex
        if (currentIndex == C.INDEX_UNSET) return emptyList()

        val uris = ArrayList<Head>(count)
        var index = currentIndex

        while (uris.size < count) {
            index = timeline.getNextWindowIndex(index, player.repeatMode, player.shuffleModeEnabled)
            if (index == C.INDEX_UNSET || index == currentIndex) break

            val mediaItem = player.getMediaItemAt(index)

            val type = mediaItem.mediaMetadata.extras?.getString("type")
            if (type == Constants.MEDIA_TYPE_RADIO || mediaItem.mediaId.startsWith("ir-")) continue

            if (DownloadUtil.getDownloadTracker(context).isDownloaded(mediaItem.mediaId)) continue

            val uri = mediaItem.localConfiguration?.uri
                ?: mediaItem.requestMetadata.mediaUri
                ?: continue

            val scheme = uri.scheme
            if (scheme != "http" && scheme != "https" && !HubTracks.isStreamUri(uri)) continue
            if (!QueueLoading.continuable(uri)) continue

            val bitrate = mediaItem.mediaMetadata.extras?.getInt("bitrate") ?: 0
            // the Hub's light variant (Opus 160) is lighter than the file the song says it is
            val light = HubLight.isLightKey(StreamingCacheKeyFactory().buildCacheKey(DataSpec.Builder().setUri(uri).build()))
            uris.add(Head(uri, QueueLoading.headBytes(HubLight.headKbps(bitrate, light), economy)))
        }

        return uris
    }

    private fun cacheTrack(context: Context, head: Head, myGeneration: Int) {
        val uri = head.uri
        forgetHeadWithoutLength(context, uri)
        // A head is continued with a Range request (only such streams are pre-cached), so a partial one left by
        // an earlier run stays: the writer fills in what is missing.
        val dataSpec = DataSpec.Builder().setUri(uri).setLength(head.bytes)
            .setHttpRequestHeaders(HubTracks.prefetchHeaders(uri)).build()
        val dataSource = DownloadUtil.getStreamingCacheWriterFactory(context).createDataSource()

        val writer = CacheWriter(dataSource, dataSpec, null, null)

        // Re-check the generation while holding the same lock cancel() takes:
        // otherwise a cancel() landing between the caller's generation check and
        // this assignment would miss the writer and the stale write would run to
        // completion.
        synchronized(this) {
            if (generation.get() != myGeneration) return
            activeWriter = writer
        }

        try {
            writer.cache()
            Log.d(TAG, "Pre-cached ${head.bytes} bytes of $uri")
        } catch (exception: Exception) {
            Log.d(TAG, "Pre-cache aborted for $uri: $exception")
        } finally {
            synchronized(this) {
                if (activeWriter === writer) activeWriter = null
            }
        }
    }

    /**
     * A head cached by an earlier version, without the file's length (`CacheLength`), is dropped: the writer would
     * only read it back from the cache and never learn the length, and the player could not seek the track.
     * Once per key, so a server that does not say the length is not asked for the same head over and over.
     */
    private fun forgetHeadWithoutLength(context: Context, uri: Uri) {
        try {
            val cache = DownloadUtil.getStreamingCacheForPreload(context)
            val cacheKey = StreamingCacheKeyFactory().buildCacheKey(DataSpec.Builder().setUri(uri).build())
            if (CacheLength.known(cache, cacheKey) || cache.getCachedBytes(cacheKey, 0, 1) <= 0) return
            if (!synchronized(relearned) { relearned.add(cacheKey) }) return
            Log.d(TAG, "Head of $cacheKey has no length, loading it again")
            cache.removeResource(cacheKey)
        } catch (exception: Exception) {
            Log.d(TAG, "Could not check the head of $uri: $exception")
        }
    }

    /** The head is in the cache (or the whole file, when it is shorter), with the file's length (once per key, [relearned]). */
    private fun isCached(context: Context, head: Head): Boolean {
        return try {
            val cache = DownloadUtil.getStreamingCacheForPreload(context)
            val cacheKey = StreamingCacheKeyFactory().buildCacheKey(DataSpec.Builder().setUri(head.uri).build())
            val contentLength = ContentMetadata.getContentLength(cache.getContentMetadata(cacheKey))
            if (contentLength == C.LENGTH_UNSET.toLong() && synchronized(relearned) { cacheKey !in relearned }) return false
            val wanted = if (contentLength == C.LENGTH_UNSET.toLong()) head.bytes else minOf(head.bytes, contentLength)
            cache.isCached(cacheKey, 0, wanted)
        } catch (exception: Exception) {
            false
        }
    }

    private fun isMetered(context: Context): Boolean {
        val connectivityManager = context.getSystemService(ConnectivityManager::class.java) ?: return true
        return connectivityManager.isActiveNetworkMetered
    }
}
