package com.eddyizm.tempus.ui.adapter;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import com.eddyizm.tempus.subsonic.models.Child;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

public class PlayerSongQueueAdapterTest {
    @Test
    public void diffContract_tracksAtDifferentQueuePositionsAreDifferentItems() {
        Child track = new Child("track-1");
        List<Child> oldQueue = Arrays.asList(track, track);
        List<Child> newQueue = Arrays.asList(track);

        androidx.recyclerview.widget.DiffUtil.DiffResult result =
                PlayerSongQueueAdapter.calculateQueueDiff(oldQueue, newQueue);

        assertTrue(result != null);
        assertFalse(PlayerSongQueueAdapter.areQueueItemsTheSame(
                oldQueue.get(0), 0, newQueue.get(0), 1));
        assertTrue(PlayerSongQueueAdapter.areQueueItemsTheSame(
                oldQueue.get(0), 0, newQueue.get(0), 0));
    }

    @Test
    public void diffContract_changedTrackContentsAreNotTheSame() {
        Child oldTrack = new Child("track-1");
        oldTrack.setTitle("Original title");
        Child newTrack = new Child("track-1");
        newTrack.setTitle("Updated title");
        List<Child> oldQueue = new ArrayList<>(Arrays.asList(oldTrack));
        List<Child> newQueue = new ArrayList<>(Arrays.asList(newTrack));

        assertTrue(PlayerSongQueueAdapter.DIFF_CALLBACK.areItemsTheSame(oldTrack, newTrack));
        assertFalse(PlayerSongQueueAdapter.DIFF_CALLBACK.areContentsTheSame(oldTrack, newTrack));
        assertTrue(PlayerSongQueueAdapter.calculateQueueDiff(oldQueue, newQueue) != null);
    }
}
