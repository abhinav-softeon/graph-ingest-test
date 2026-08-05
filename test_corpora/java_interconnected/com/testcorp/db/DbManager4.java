package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 4. Not a JDBC type, but getConnection() returns a Connection. */
public class DbManager4 {
    private static final String URL = "jdbc:h2:mem:test4";

    public Connection getConnection() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
