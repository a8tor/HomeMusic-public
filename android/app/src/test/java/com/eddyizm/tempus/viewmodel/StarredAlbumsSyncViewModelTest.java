package com.eddyizm.tempus.viewmodel;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.eddyizm.tempus.subsonic.models.Child;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.JUnit4;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

@RunWith(JUnit4.class)
public class StarredAlbumsSyncViewModelTest {
    @Test
    public void zeroAlbumsEmitsEmptyResult() {
        AtomicReference<List<Child>> result = new AtomicReference<>();
        new StarredAlbumsSyncViewModel.AlbumSongsAccumulator(0, result::set);
        assertNotNull(result.get());
        assertTrue(result.get().isEmpty());
    }

    @Test
    public void oneAlbumEmitsItsSongs() {
        AtomicReference<List<Child>> result = new AtomicReference<>();
        Child song = new Child("song");
        StarredAlbumsSyncViewModel.AlbumSongsAccumulator accumulator =
                new StarredAlbumsSyncViewModel.AlbumSongsAccumulator(1, result::set);
        accumulator.onAlbumSongs(0, Collections.singletonList(song));
        assertEquals(Collections.singletonList(song), result.get());
    }

    @Test
    public void multipleAlbumsWaitForEveryResult() {
        AtomicReference<List<Child>> result = new AtomicReference<>();
        Child first = new Child("first");
        Child second = new Child("second");
        StarredAlbumsSyncViewModel.AlbumSongsAccumulator accumulator =
                new StarredAlbumsSyncViewModel.AlbumSongsAccumulator(2, result::set);
        accumulator.onAlbumSongs(0, Collections.singletonList(first));
        assertEquals(null, result.get());
        accumulator.onAlbumSongs(1, Collections.singletonList(second));
        assertEquals(Arrays.asList(first, second), result.get());
    }

    @Test
    public void repeatedAlbumEmissionDoesNotDuplicateSongsOrCallback() {
        AtomicInteger callbackCount = new AtomicInteger();
        StarredAlbumsSyncViewModel.AlbumSongsAccumulator accumulator =
                new StarredAlbumsSyncViewModel.AlbumSongsAccumulator(1, ignored -> callbackCount.incrementAndGet());
        accumulator.onAlbumSongs(0, Collections.singletonList(new Child("first")));
        accumulator.onAlbumSongs(0, Collections.singletonList(new Child("duplicate")));
        assertEquals(1, callbackCount.get());
    }

    @Test
    public void parallelAlbumResponsesAreAggregatedExactlyOnce() throws Exception {
        AtomicInteger callbackCount = new AtomicInteger();
        AtomicReference<List<Child>> result = new AtomicReference<>();
        StarredAlbumsSyncViewModel.AlbumSongsAccumulator accumulator =
                new StarredAlbumsSyncViewModel.AlbumSongsAccumulator(8, songs -> {
                    result.set(songs);
                    callbackCount.incrementAndGet();
                });
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch finished = new CountDownLatch(8);
        List<Thread> threads = new ArrayList<>();
        List<Child> expected = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            final int index = i;
            Child song = new Child("song-" + i);
            expected.add(song);
            Thread thread = new Thread(() -> {
                try {
                    start.await();
                    accumulator.onAlbumSongs(index, Collections.singletonList(song));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    finished.countDown();
                }
            });
            threads.add(thread);
            thread.start();
        }
        start.countDown();
        assertTrue(finished.await(5, TimeUnit.SECONDS));
        assertEquals(8, result.get().size());
        assertTrue(result.get().containsAll(expected));
        assertEquals(1, callbackCount.get());
    }
}
