package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 5. Not a JDBC type, but getDbConn() returns a Connection. */
public class DbManager5 {
    private static final String URL = "jdbc:h2:mem:test5";

    public Connection getDbConn() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
