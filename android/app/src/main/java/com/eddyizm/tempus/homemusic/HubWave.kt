package com.eddyizm.tempus.homemusic

import android.util.Log
import android.view.View
import android.widget.Toast
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import com.eddyizm.tempus.App
import com.eddyizm.tempus.R
import com.eddyizm.tempus.lan.LanRemoteSession
import com.eddyizm.tempus.service.MediaManager
import com.eddyizm.tempus.service.QueuePreloader
import com.eddyizm.tempus.subsonic.models.Child
import com.eddyizm.tempus.subsonic.utils.CacheUtil
import com.eddyizm.tempus.ui.activity.MainActivity
import com.eddyizm.tempus.util.MappingUtil
import java.lang.ref.WeakReference

/**
 * «Моя волна» in the app's player (MIXES.md 7): plays a wave's first tracks and keeps the queue going by itself —
 * on the start of every wave track (and after a ♥ or «Не нравится» of one) it gives the Hub the events since the
 * last call and the tracks still ahead (`POST /api/wave/{id}/next`), and applies the Hub's queue delta. Offline,
 * the queue stays as it is and the call is tried again later. Another queue ends the wave.
 * The queue is edited on the service's player ([attach]), so it goes on with the app in the background.
 * Everything here runs on the main thread, the calls on [HomeMusic.io].
 */
@androidx.annotation.OptIn(markerClass = [UnstableApi::class])
object HubWave {
    private const val TAG = "HubWave"

    private var target = WeakReference<MediaManager.QueueTarget>(null)
    private var session: WaveSession? = null
    private var retry: Runnable? = null
    private var emptyQueueSessionId: String? = null

    /** The playback service: the wave edits its player. */
    @JvmStatic
    fun attach(service: MediaManager.QueueTarget) {
        target = WeakReference(service)
    }

    // --- starting -------------------------------------------------------------------------------

    /** `HomeMusicAndroid.playWave`: the page started a wave (`POST /api/wave/start`) and hands over its answer. */
    fun play(activity: MainActivity, out: WaveOut) {
        val id = out.sessionId?.takeIf { it.isNotBlank() }
        val batch = out.newTracks()
        val refs = batch.map { it.ref.orEmpty() }
        val plan = out.keep?.takeIf { out.add != null }?.let { WavePlan.ofDelta(emptyList(), it, refs) }
            ?: WavePlan.of(emptyList(), refs)
        val tracks = plan.add.map { batch[it] }
        if (id == null || tracks.isEmpty()) {
            Toast.makeText(activity, R.string.homemusic_wave_empty, Toast.LENGTH_SHORT).show()
            return
        }
        cancelRetry()
        val wave = WaveSession(id, out.title, out.seed, out.mode)
        wave.register(tracks)
        session = wave
        // the start of its first track asks for what follows
        HubPlayer.playQueue(activity, tracks.map { it.item(wave.context) }, 0)
    }

    /**
     * Starts a wave from the app («Моя волна» on Главная, «Волна по треку» in the track menu, «Радиостанции»):
     * [seed] — `{ref}`, `{nd_song_id}`, `{artist_ref}`, `{nd_artist_id}` or null for the user's own. The Hub
     * failed: [fallback] when given, else a toast. [origin] — where it was started from ([WaveOrigin]).
     */
    fun start(activity: MainActivity, seed: Map<String, String>?, fallback: Runnable? = null, origin: String? = null) {
        val wave = session
        val livePlayer = target.get()?.livePlayer()
        if (wave != null && livePlayer != null && isSameSeed(wave.seed, seed) && wave.isWave(livePlayer.currentMediaItem?.mediaId)) {
            if (!livePlayer.isPlaying) {
                livePlayer.play()
            }
            activity.expandBottomSheet()
            return
        }

        Toast.makeText(activity, R.string.homemusic_wave_starting, Toast.LENGTH_SHORT).show()
        HomeMusic.io.execute {
            val result = runCatching {
                // origin and client only for a Hub that knows them (wave_origin_v1)
                val known = HubClient.supportsWaveOrigin()
                HubClient.waveStart(WaveSession.startBody(seed, null, origin.takeIf { known }, null, HubPlays.client.takeIf { known }))
            }
            HomeMusic.main.post {
                if (activity.isFinishing || activity.isDestroyed) return@post
                result.onSuccess { play(activity, it) }.onFailure { e ->
                    Log.w(TAG, "start", e)
                    if (fallback != null) return@onFailure fallback.run()
                    val text = (e as? HubException)?.message ?: activity.getString(R.string.homemusic_wave_failed)
                    Toast.makeText(activity, text, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    /** «Радиостанции» on Главная: a wave by a Navidrome artist; offline or the Hub failed — [fallback]. */
    @JvmStatic
    fun startArtist(activity: MainActivity, artistId: String, fallback: Runnable) {
        if (!CacheUtil.isConnected()) return fallback.run()
        start(activity, mapOf("nd_artist_id" to artistId), fallback, WaveOrigin.RADIO)
    }

    /** «Волна по треку» for a queue entry: a catalog track by its ref, a library song by its id. */
    fun seedOf(song: Child): Map<String, String>? = WaveSeeds.track(song.id)

    fun artistSeedOf(song: Child): Map<String, String>? = WaveSeeds.artist(song.id, song.artistId)

    @JvmStatic
    fun startArtistExact(activity: MainActivity, id: String, origin: String) {
        if (id.isNotBlank()) start(activity, mapOf("nd_artist_id" to id), null, origin)
    }

    @JvmStatic
    fun bindArtistSheetRow(row: View?, song: Child, dismiss: Runnable, fromPlayer: Boolean) =
        bindRow(row, song, dismiss, true, fromPlayer)

    /** The «Волна по треку» row of the track menu; [fromPlayer] — the menu was opened in the player. */
    @JvmStatic
    fun bindSheetRow(row: View?, song: Child, dismiss: Runnable, fromPlayer: Boolean) =
        bindRow(row, song, dismiss, false, fromPlayer)

    /** Where a wave started from the track menu was started: the player's menu is «player», the others by the row. */
    fun menuOrigin(artist: Boolean, fromPlayer: Boolean): String =
        if (fromPlayer) WaveOrigin.PLAYER else if (artist) WaveOrigin.ARTIST_MENU else WaveOrigin.TRACK_MENU

    private fun bindRow(row: View?, song: Child, dismiss: Runnable, artist: Boolean, fromPlayer: Boolean) {
        row ?: return
        val seed = if (artist) artistSeedOf(song) else seedOf(song)
        if (!HubDislikes.applies(song) || seed == null) {
            row.visibility = View.GONE
            return
        }
        row.visibility = View.VISIBLE
        row.setOnClickListener {
            val activity = HubDislikes.activityOf(it.context)
            dismiss.run()
            if (activity != null) start(activity, seed, null, menuOrigin(artist, fromPlayer))
        }
    }

    // --- what the player does --------------------------------------------------------------------

    /** A catalog track of the wave had only a 30 s preview (503 / preview header): tell its captured session. */
    @JvmStatic
    fun onNoFile(sessionId: String, ref: String) {
        val cleanRef = ref.trim().removePrefix(HubTracks.ID_PREFIX)
        if (sessionId.isBlank() || cleanRef.isBlank()) return
        HomeMusic.io.execute {
            runCatching { HubClient.waveNoFile(sessionId, cleanRef) }
        }
    }

    /** The failed item is removed; ask immediately when it was the last queued wave track. */
    fun onTrackUnavailable(player: Player, mediaId: String, sessionId: String, ref: String) {
        HomeMusic.main.post {
            val wave = session?.takeIf { it.id == sessionId && it.context == HubPlays.contextOf(mediaId) } ?: return@post
            if (!wave.isWave(mediaId) || player.currentMediaItem?.mediaId != mediaId) return@post
            onNoFile(sessionId, ref)
            val queue = target.get() ?: return@post
            val index = player.currentMediaItemIndex
            if (index == C.INDEX_UNSET) return@post
            val hasNext = player.hasNextMediaItem()
            if (hasNext) {
                player.seekToNextMediaItem()
                MediaManager.removeRange(queue, index, index + 1)
            } else {
                emptyQueueSessionId = sessionId
                MediaManager.removeRange(queue, index, index + 1)
                ask()
            }
        }
    }

    /** A new queue started; [contexts] — its entries' contexts: the wave goes on only when it is the wave's own. */
    fun onQueueStarted(contexts: Map<String, String>) {
        HomeMusic.main.post {
            val wave = session ?: return@post
            if (contexts.values.none { it == wave.context }) end()
        }
    }

    /** Every listening event ([HubPlays.record]). */
    fun onEvent(event: PlayEvent) {
        HomeMusic.main.post {
            if (session?.onEvent(event) == true) ask()
        }
    }

    /** «Не нравится» went straight to the Hub (`/api/dislikes`); a wave track ahead goes at the next answer. */
    @JvmStatic
    fun onDisliked(mediaId: String) {
        HomeMusic.main.post {
            if (session?.isWave(mediaId) == true) ask()
        }
    }

    /** The queue played out (nothing ahead came in time): ask now so the wave can play on. */
    fun onQueueEnded() {
        HomeMusic.main.post {
            val wave = session ?: return@post
            if (!wave.inFlight) ask()
        }
    }

    /** The wave plays on [player] now: its queue is the wave's to keep going ([QueueAhead] stays out of it). */
    @JvmStatic
    fun owns(player: Player): Boolean = session?.isWave(player.currentMediaItem?.mediaId) == true

    /** The user waits on the loading page after the last track ([QueueAhead.requestNext]). */
    @JvmStatic
    fun askNow() {
        HomeMusic.main.post {
            val wave = session ?: return@post
            wave.failures = 0
            ask()
        }
    }

    /**
     * The precache may load the heads of the tracks ahead of [player]'s: not while a wave track plays and the Hub
     * has not yet answered for it (the answer replaces the queue ahead, and the heads loaded before would be lost).
     */
    @JvmStatic
    fun preloadReady(player: Player): Boolean {
        val wave = session ?: return true
        val id = player.currentMediaItem?.mediaId
        return !wave.isWave(id) || wave.settled(id)
    }

    /** The answer is in (or the Hub does not answer): the precache may begin. */
    private fun kickPreload(wave: WaveSession) {
        val player = player(wave) ?: return
        QueuePreloader.preload(App.getContext(), player)
    }

    private fun end() {
        session = null
        emptyQueueSessionId = null
        cancelRetry()
    }

    // --- next -----------------------------------------------------------------------------------

    /** The service's player while it plays the wave (or its queue is empty after an unavailable track). */
    private fun player(wave: WaveSession): Player? {
        if (LanRemoteSession.isActive()) return null
        val player = target.get()?.livePlayer() ?: return null
        return player.takeIf {
            wave.isWave(it.currentMediaItem?.mediaId) ||
                (it.mediaItemCount == 0 && emptyQueueSessionId == wave.id)
        }
    }

    /** The wave's queue entries after the one playing, in order, with their positions. */
    private fun ahead(player: Player, wave: WaveSession): List<Pair<Int, String>> {
        val current = player.currentMediaItemIndex.coerceAtLeast(0)
        return (current + 1 until player.mediaItemCount)
            .map { it to player.getMediaItemAt(it).mediaId }
            .filter { wave.isWave(it.second) }
    }

    private fun ask() {
        val wave = session ?: return
        if (wave.inFlight) {
            wave.again = true
            return
        }
        val player = player(wave) ?: return
        cancelRetry()
        val request = wave.snapshot(ahead(player, wave).map { it.second })
        val id = wave.id
        wave.askedFor = player.currentMediaItem?.mediaId
        wave.inFlight = true
        wave.again = false
        HomeMusic.io.execute {
            var sent = request
            val result = runCatching {
                val account = CatalogFavorites.currentAccount()
                sent = request.compatible(HubClient.supportsTypedFavorites(), HubClient.supportsCompensationChains(), account)
                val out = if (sent.played.any { it.account != null })
                    HubClient.withAccountSession(requireNotNull(account)) { HubClient.waveNext(id, sent) }
                else HubClient.waveNext(id, sent)
                out to prepare(out.newTracks(), wave.context)
            }
            HomeMusic.main.post { answered(wave, sent, result) }
        }
    }

    /** A track and how it goes into the queue (mapped on this worker thread: the mapping reads the database). */
    private class Prepared(val track: WaveTrack, val child: Child, val item: MediaItem)

    private fun prepare(tracks: List<WaveTrack>, context: String): List<Prepared> {
        val items = tracks.map { it.item(context) }
        return HubPlayer.resolve(items).mapIndexedNotNull { i, child ->
            child ?: return@mapIndexedNotNull null
            val item = MappingUtil.mapMediaItems(listOf(child)).firstOrNull() ?: return@mapIndexedNotNull null
            Prepared(tracks[i], child, item)
        }
    }

    private fun answered(wave: WaveSession, request: WaveNextIn, result: Result<Pair<WaveOut, List<Prepared>>>) {
        wave.inFlight = false
        if (session !== wave) return
        result.onSuccess { (out, prepared) ->
            wave.sent(request)
            wave.failures = 0
            apply(wave, out, prepared)
            wave.settledFor = wave.askedFor
            if (wave.again) {
                ask()
                return
            }
            kickPreload(wave)
            val left = player(wave)?.let { ahead(it, wave).size } ?: return
            if (prepared.isEmpty() && out.pending && left <= WaveSession.LOW_WATER) {
                wave.polls++
                WaveRetry.afterPending(wave.polls)?.let(::schedule)
            } else {
                wave.polls = 0
            }
        }.onFailure { e ->
            if (e is HubException && e.code == 404) {
                renew(wave)
                return
            }
            Log.w(TAG, "next ${wave.id}: ${e.message}")
            if (e !is HubException) HubClient.invalidate()
            wave.failures++
            kickPreload(wave)
            val left = player(wave)?.let { ahead(it, wave).size } ?: return
            if (wave.again || left <= WaveSession.LOW_WATER) WaveRetry.afterFailure(wave.failures)?.let(::schedule)
        }
    }

    /** Edit only the upcoming wave entries; a response may arrive after playback has advanced. */
    private fun apply(wave: WaveSession, out: WaveOut, prepared: List<Prepared>) {
        val queue = target.get() ?: return
        val player = player(wave) ?: return
        val ahead = ahead(player, wave)
        val queued = ahead.map { wave.refOf(it.second).orEmpty() }
        val incoming = prepared.map { it.track.ref.orEmpty() }
        val keep = out.keep?.takeIf { out.add != null }
        val plan = if (keep != null) WavePlan.ofDelta(queued, keep, incoming) else WavePlan.of(queued, incoming)
        val currentRef = player.currentMediaItem?.mediaId?.let(wave::refOf)
        val add = plan.add.map { prepared[it] }.filter { it.track.ref != currentRef }
        val resume = player.playbackState == Player.STATE_ENDED || emptyQueueSessionId == wave.id
        val wasEmpty = player.mediaItemCount == 0
        val at = plan.insertAt(ahead.map { it.first }, player.mediaItemCount)
        for (i in plan.remove.reversed()) {
            val index = ahead[i].first
            MediaManager.removeRange(queue, index, index + 1)
        }
        if (add.isNotEmpty()) {
            wave.register(add.map { it.track })
            HubPlays.addContexts(wave.ids().associateWith { wave.context }, launchesOf(wave))
            MediaManager.insertAt(queue, at, add.map { it.child }, add.map { it.item })
            emptyQueueSessionId = null
        }
        val next = if (wasEmpty) 0 else player.currentMediaItemIndex + 1
        if (resume && next in 0 until player.mediaItemCount) {
            // Adding items can change STATE_ENDED. Resume based on the state before editing.
            player.seekToDefaultPosition(next)
            player.prepare()
            player.play()
            QueueAhead.extended(player, C.INDEX_UNSET, 1)
        } else if (add.isNotEmpty()) {
            // Honour an explicit request from the loading page, without restarting the current item.
            QueueAhead.extended(player, at, add.size)
        }
    }

    /** The Hub forgot the session (24 h, a long offline): a new one with the same seed carries the queue on. */
    private fun renew(wave: WaveSession) {
        val oldId = wave.id
        wave.inFlight = true
        HomeMusic.io.execute {
            val result = runCatching {
                val known = HubClient.supportsWaveOrigin()
                val body = WaveSession.startBody(wave.seed, wave.mode, WaveOrigin.RESTART.takeIf { known }, oldId.takeIf { known },
                    HubPlays.client.takeIf { known })
                val out = HubClient.waveStart(body)
                // a track's wave starts with that track again: it has played already
                val tracks = out.newTracks().filter { it.generator != WaveTrack.SEED }
                val id = out.sessionId ?: throw HubException("Пустой ответ Хаба")
                Triple(id, out, prepare(tracks, WaveSession.CONTEXT_PREFIX + id))
            }
            HomeMusic.main.post {
                wave.inFlight = false
                if (session !== wave) return@post
                result.onSuccess { (id, out, prepared) ->
                    val resumeEmpty = emptyQueueSessionId == oldId
                    wave.renew(id)
                    if (resumeEmpty) emptyQueueSessionId = id
                    HubPlays.addContexts(wave.ids().associateWith { wave.context }, launchesOf(wave))
                    wave.settledFor = player(wave)?.currentMediaItem?.mediaId
                    apply(wave, out, prepared)
                    if (wave.again) ask() else kickPreload(wave)
                }.onFailure { e ->
                    Log.w(TAG, "renew: ${e.message}")
                    wave.failures++
                    WaveRetry.afterFailure(wave.failures)?.let(::schedule)
                }
            }
        }
    }

    /** The launch of every track of [wave] so far: `wave:<session>` (the same one as the context). */
    private fun launchesOf(wave: WaveSession): Map<String, Launch> {
        val launch = Launch.of(wave.context, WaveOrigin.WAVE) ?: return emptyMap()
        return wave.ids().associateWith { launch }
    }

    private fun schedule(delayMs: Long) {
        cancelRetry()
        val run = Runnable {
            retry = null
            ask()
        }
        retry = run
        HomeMusic.main.postDelayed(run, delayMs)
    }

    private fun cancelRetry() {
        retry?.let(HomeMusic.main::removeCallbacks)
        retry = null
    }

    fun isSameSeed(s1: Map<String, Any?>?, s2: Map<String, String>?): Boolean {
        val empty1 = s1.isNullOrEmpty() || s1.values.all { it == null || it == "" }
        val empty2 = s2.isNullOrEmpty() || s2.values.all { it.isBlank() }
        if (empty1 && empty2) return true
        if (empty1 != empty2) return false

        val keys = setOf("ref", "nd_song_id", "artist_ref", "nd_artist_id")
        for (k in keys) {
            val v1 = s1?.get(k)?.toString()?.takeIf { it.isNotBlank() }
            val v2 = s2?.get(k)?.takeIf { it.isNotBlank() }
            if (v1 != v2) return false
        }
        val b1 = s1?.get("by_artist").let { it == true || it == "true" }
        val b2 = s2?.get("by_artist") == "true"
        return b1 == b2
    }
}
