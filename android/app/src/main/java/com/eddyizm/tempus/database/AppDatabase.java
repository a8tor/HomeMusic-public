package com.eddyizm.tempus.database;

import android.database.sqlite.SQLiteDatabase;
import android.util.Log;

import androidx.annotation.NonNull;
import androidx.media3.common.util.UnstableApi;
import androidx.room.migration.Migration;
import androidx.sqlite.db.SupportSQLiteDatabase;
import androidx.room.AutoMigration;
import androidx.room.Database;
import androidx.room.Room;
import androidx.room.RoomDatabase;
import androidx.room.TypeConverters;

import com.eddyizm.tempus.App;
import com.eddyizm.tempus.database.converter.DateConverters;
import com.eddyizm.tempus.database.converter.StringListConverter;
import com.eddyizm.tempus.database.dao.ChronologyDao;
import com.eddyizm.tempus.database.dao.DownloadDao;
import com.eddyizm.tempus.database.dao.FavoriteDao;
import com.eddyizm.tempus.database.dao.InternetRadioStationDao;
import com.eddyizm.tempus.database.dao.LyricsDao;
import com.eddyizm.tempus.database.dao.PinnedPlaylistDao;
import com.eddyizm.tempus.database.dao.SyncedPlaylistDao;
import com.eddyizm.tempus.database.dao.PlaylistDao;
import com.eddyizm.tempus.database.dao.PlaylistSongDao;
import com.eddyizm.tempus.database.dao.QueueDao;
import com.eddyizm.tempus.database.dao.RecentSearchDao;
import com.eddyizm.tempus.database.dao.ServerDao;
import com.eddyizm.tempus.database.dao.SessionMediaItemDao;
import com.eddyizm.tempus.model.Chronology;
import com.eddyizm.tempus.model.Download;
import com.eddyizm.tempus.model.Favorite;
import com.eddyizm.tempus.model.InternetRadioStationCache;
import com.eddyizm.tempus.model.LyricsCache;
import com.eddyizm.tempus.model.PinnedPlaylist;
import com.eddyizm.tempus.model.PlaylistSong;
import com.eddyizm.tempus.model.Queue;
import com.eddyizm.tempus.model.RecentSearch;
import com.eddyizm.tempus.model.Server;
import com.eddyizm.tempus.model.SessionMediaItem;
import com.eddyizm.tempus.model.SyncedPlaylist;
import com.eddyizm.tempus.subsonic.models.Playlist;

@UnstableApi
@Database(
        version = 24,
        entities = {
            Queue.class,
            Server.class,
            RecentSearch.class,
            Download.class,
            Chronology.class,
            Favorite.class,
            SessionMediaItem.class,
            Playlist.class,
            PinnedPlaylist.class,
            LyricsCache.class,
            InternetRadioStationCache.class,
            PlaylistSong.class,
            SyncedPlaylist.class,
        },
        autoMigrations = {
                @AutoMigration(from = 10, to = 11),
                @AutoMigration(from = 11, to = 12),
                @AutoMigration(from = 12, to = 13),
                @AutoMigration(from = 13, to = 14),
                @AutoMigration(from = 14, to = 15),
                @AutoMigration(from = 15, to = 16),
                @AutoMigration(from = 16, to = 17),
                @AutoMigration(from = 17, to = 18),
                @AutoMigration(from = 18, to = 19),
                @AutoMigration(from = 19, to = 20),
                @AutoMigration(from = 20, to = 21),
                @AutoMigration(from = 21, to = 22),
                @AutoMigration(from = 22, to = 23),
        }
)
@TypeConverters({DateConverters.class, StringListConverter.class})
public abstract class AppDatabase extends RoomDatabase {
    private static final String TAG = "AppDatabase";
    private static final String DB_NAME = "tempo_db";
    public static final Migration MIGRATION_23_24 = new Migration(23, 24) {
        @Override
        public void migrate(@NonNull SupportSQLiteDatabase database) {
            database.execSQL("CREATE INDEX IF NOT EXISTS `index_chronology_server_timestamp` "
                    + "ON `chronology` (`server`, `timestamp`)");
        }
    };
    private static AppDatabase instance;

    public static synchronized AppDatabase getInstance() {
        if (instance == null) {
            logDatabaseVersions();
            instance = Room.databaseBuilder(App.getContext(), AppDatabase.class, DB_NAME)
                    .addMigrations(MIGRATION_23_24)
                    .fallbackToDestructiveMigration()
                    .build();
        }

        return instance;
    }

    static void logDatabaseVersions() {
        try {
            android.content.Context context = App.getContext();
            logDatabaseVersions(context == null ? null : context.getDatabasePath(DB_NAME));
        } catch (Exception exception) {
            Log.w(TAG, "Unable to resolve database path before opening; continuing", exception);
        }
    }

    static void logDatabaseVersions(java.io.File databaseFile) {
        int installedSchemaVersion = -1;
        if (databaseFile == null) {
            Log.i(TAG, "Database path unavailable; skipping installed schema version check; target schema version=24");
            return;
        }
        try {
            if (databaseFile.exists()) {
                try (SQLiteDatabase database = SQLiteDatabase.openDatabase(
                        databaseFile.getAbsolutePath(), null, SQLiteDatabase.OPEN_READONLY)) {
                    installedSchemaVersion = database.getVersion();
                }
            }
        } catch (Exception exception) {
            Log.w(TAG, "Unable to read database schema version before opening; continuing", exception);
        }
        Log.i(TAG, "Opening database; installed schema version=" + installedSchemaVersion
                + ", target schema version=24");
    }

    public abstract QueueDao queueDao();

    public abstract ServerDao serverDao();

    public abstract RecentSearchDao recentSearchDao();

    public abstract DownloadDao downloadDao();

    public abstract ChronologyDao chronologyDao();

    public abstract FavoriteDao favoriteDao();

    public abstract SessionMediaItemDao sessionMediaItemDao();

    public abstract PlaylistDao playlistDao();

    public abstract PinnedPlaylistDao pinnedPlaylistDao();

    public abstract SyncedPlaylistDao syncedPlaylistDao();

    public abstract PlaylistSongDao playlistSongDao();

    public abstract LyricsDao lyricsDao();

    public abstract InternetRadioStationDao internetRadioStationDao();
}
