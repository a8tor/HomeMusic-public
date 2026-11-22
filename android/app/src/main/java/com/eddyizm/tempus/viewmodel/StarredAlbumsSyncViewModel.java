package com.eddyizm.tempus.viewmodel;

import android.app.Application;
import android.app.Activity;

import androidx.annotation.NonNull;
import androidx.lifecycle.AndroidViewModel;
import androidx.lifecycle.LifecycleOwner;
import androidx.lifecycle.LiveData;
import androidx.lifecycle.Observer;
import androidx.lifecycle.MutableLiveData;

import com.eddyizm.tempus.repository.AlbumRepository;
import com.eddyizm.tempus.subsonic.models.AlbumID3;
import com.eddyizm.tempus.subsonic.models.Child;

import java.util.ArrayList;
import java.util.List;

public class StarredAlbumsSyncViewModel extends AndroidViewModel {
    private final AlbumRepository albumRepository;

    private final MutableLiveData<List<AlbumID3>> starredAlbums = new MutableLiveData<>(null);
    private final MutableLiveData<List<Child>> starredAlbumSongs = new MutableLiveData<>(null);

    public StarredAlbumsSyncViewModel(@NonNull Application application) {
        super(application);
        albumRepository = new AlbumRepository();
    }

    public LiveData<List<AlbumID3>> getStarredAlbums(LifecycleOwner owner) {
        albumRepository.getStarredAlbums(false, -1).observe(owner, starredAlbums::postValue);
        return starredAlbums;
    }

    public LiveData<List<Child>> getAllStarredAlbumSongs() {
        LiveData<List<AlbumID3>> albumsLiveData = albumRepository.getStarredAlbums(false, -1);
        albumsLiveData.observeForever(new Observer<List<AlbumID3>>() {
            @Override
            public void onChanged(List<AlbumID3> albums) {
                if (albums != null && !albums.isEmpty()) {
                    collectAllAlbumSongs(albums, starredAlbumSongs::postValue);
                } else {
                    starredAlbumSongs.postValue(new ArrayList<>());
                }
                albumsLiveData.removeObserver(this);
            }
        });
        
        return starredAlbumSongs;
    }

    public LiveData<List<Child>> getStarredAlbumSongs(Activity activity) {
        albumRepository.getStarredAlbums(false, -1).observe((LifecycleOwner) activity, albums -> {
            if (albums != null && !albums.isEmpty()) {
                collectAllAlbumSongs(albums, starredAlbumSongs::postValue);
            } else {
                starredAlbumSongs.postValue(new ArrayList<>());
            }
        });
        return starredAlbumSongs;
    }

    static final class AlbumSongsAccumulator {
        private final List<Child> allSongs = new ArrayList<>();
        private final boolean[] completedAlbums;
        private final AlbumSongsCallback callback;
        private int remaining;

        AlbumSongsAccumulator(int albumCount, AlbumSongsCallback callback) {
            this.remaining = albumCount;
            this.completedAlbums = new boolean[albumCount];
            this.callback = callback;
            if (albumCount == 0) callback.onSongsCollected(new ArrayList<>());
        }

        void onAlbumSongs(int albumIndex, List<Child> songs) {
            List<Child> completedSongs = null;
            synchronized (this) {
                if (completedAlbums[albumIndex]) return;
                completedAlbums[albumIndex] = true;
                if (songs != null) allSongs.addAll(songs);
                if (--remaining == 0) completedSongs = new ArrayList<>(allSongs);
            }
            if (completedSongs != null) callback.onSongsCollected(completedSongs);
        }
    }

    private void collectAllAlbumSongs(List<AlbumID3> albums, AlbumSongsCallback callback) {
        if (albums.isEmpty()) {
            callback.onSongsCollected(new ArrayList<>());
            return;
        }
        AlbumSongsAccumulator accumulator = new AlbumSongsAccumulator(albums.size(), callback);
        for (int index = 0; index < albums.size(); index++) {
            AlbumID3 album = albums.get(index);
            final int albumIndex = index;
            LiveData<List<Child>> albumTracks = albumRepository.getAlbumTracks(album.getId());
            albumTracks.observeForever(new Observer<List<Child>>() {
                @Override
                public void onChanged(List<Child> songs) {
                    if (songs == null) return;
                    albumTracks.removeObserver(this);
                    accumulator.onAlbumSongs(albumIndex, songs);
                }
            });
        }
    }

    interface AlbumSongsCallback {
        void onSongsCollected(List<Child> songs);
    }
}