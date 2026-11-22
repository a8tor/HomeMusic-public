package com.eddyizm.tempus.database.dao;

import androidx.lifecycle.LiveData;
import androidx.room.Dao;
import androidx.room.Insert;
import androidx.room.OnConflictStrategy;
import androidx.room.Query;

import com.eddyizm.tempus.model.Chronology;

import java.util.List;

@Dao
public interface ChronologyDao {
    @Query("SELECT * FROM chronology WHERE server == :server ORDER BY timestamp DESC, id ASC LIMIT :count")
    LiveData<List<Chronology>> getLastPlayed(String server, int count);

    @Query("SELECT * FROM chronology WHERE server == :server AND timestamp >= :lowerInclusive AND timestamp < :upperExclusive ORDER BY timestamp DESC, id ASC LIMIT 20")
    LiveData<List<Chronology>> getAllFrom(long upperExclusive, long lowerInclusive, String server);

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    void insert(Chronology chronologyObject);
}