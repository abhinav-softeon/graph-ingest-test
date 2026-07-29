package com.acme.data;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

public class AnimalRepository {
    public ResultSet findAllAnimals(Connection conn) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM animals WHERE active = 1");
    }

    public void insertAnimal(Connection conn, String name) throws Exception {
        Statement stmt = conn.createStatement();
        stmt.executeUpdate("INSERT INTO animals (name) VALUES ('" + name + "')");
    }
}
