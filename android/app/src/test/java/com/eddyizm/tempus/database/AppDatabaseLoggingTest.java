package com.eddyizm.tempus.database;


import org.junit.Test;

public class AppDatabaseLoggingTest {
    @Test
    public void logDatabaseVersionsWithNullPathDoesNotThrow() {
        AppDatabase.logDatabaseVersions(null);
    }
}
