package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 6. Not a JDBC type, but getCon() returns a Connection. */
public class DbManager6 {
    private static final String URL = "jdbc:h2:mem:test6";

    public Connection getCon() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
