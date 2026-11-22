package com.eddyizm.tempus.database;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import androidx.sqlite.db.SupportSQLiteDatabase;

import org.junit.Test;

import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;

public class ChronologyMigrationTest {
    @Test
    public void migration23To24CreatesIndexWithoutChangingRows() {
        List<String> rows = new ArrayList<>(Arrays.asList("one", "two"));
        List<String> executedSql = new ArrayList<>();
        SupportSQLiteDatabase database = (SupportSQLiteDatabase) Proxy.newProxyInstance(
                SupportSQLiteDatabase.class.getClassLoader(),
                new Class<?>[]{SupportSQLiteDatabase.class},
                (proxy, method, args) -> {
                    if ("execSQL".equals(method.getName())) executedSql.add((String) args[0]);
                    return null;
                });

        AppDatabase.MIGRATION_23_24.migrate(database);

        assertEquals(Arrays.asList("CREATE INDEX IF NOT EXISTS `index_chronology_server_timestamp` "
                + "ON `chronology` (`server`, `timestamp`)"), executedSql);
        assertEquals(Arrays.asList("one", "two"), rows);
    }

    @Test
    public void chronologyRangeOrdersRowsAndUsesHalfOpenBounds() {
        String query = "SELECT * FROM chronology WHERE server == :server "
                + "AND timestamp >= :lowerInclusive AND timestamp < :upperExclusive "
                + "ORDER BY timestamp DESC, id ASC LIMIT 20";
        assertTrue(query.contains("timestamp >= :lowerInclusive"));
        assertTrue(query.contains("timestamp < :upperExclusive"));
        assertTrue(query.contains("ORDER BY timestamp DESC, id ASC"));

        List<Played> rows = Arrays.asList(
                new Played("b", 20, "server"),
                new Played("a", 20, "server"),
                new Played("old", 10, "server"),
                new Played("upper", 30, "server"),
                new Played("other-server", 25, "other"));
        List<String> ids = new ArrayList<>();
        rows.stream()
                .filter(row -> "server".equals(row.server) && row.timestamp >= 10 && row.timestamp < 30)
                .sorted(Comparator.comparingLong((Played row) -> row.timestamp).reversed()
                        .thenComparing(row -> row.id))
                .limit(20)
                .forEach(row -> ids.add(row.id));

        assertEquals(Arrays.asList("a", "b", "old"), ids);
    }

    private static final class Played {
        final String id;
        final long timestamp;
        final String server;

        Played(String id, long timestamp, String server) {
            this.id = id;
            this.timestamp = timestamp;
            this.server = server;
        }
    }
}
