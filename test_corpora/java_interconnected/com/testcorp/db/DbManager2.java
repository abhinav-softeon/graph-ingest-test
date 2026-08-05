package com.testcorp.db;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

/** Pool wrapper 2. Not a JDBC type, but getCon() returns a Connection. */
public class DbManager2 {
    private static final String URL = "jdbc:h2:mem:test2";

    public Connection getCon() throws SQLException {
        return DriverManager.getConnection(URL);
    }

    public boolean healthy() {
        return URL.length() > 0;
    }
}
