package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 3. Not a JDBC type, but getDbConnection() returns a Connection. */
public class DbManager3 {
    private static final String URL = "jdbc:h2:mem:test3";

    public Connection getDbConnection() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
