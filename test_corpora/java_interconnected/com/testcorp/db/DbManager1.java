package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 1. Not a JDBC type, but getDbConn() returns a Connection. */
public class DbManager1 {
    private static final String URL = "jdbc:h2:mem:test1";

    public Connection getDbConn() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
