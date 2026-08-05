package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 7. Not a JDBC type, but getDbConnection() returns a Connection. */
public class DbManager7 {
    private static final String URL = "jdbc:h2:mem:test7";

    public Connection getDbConnection() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
