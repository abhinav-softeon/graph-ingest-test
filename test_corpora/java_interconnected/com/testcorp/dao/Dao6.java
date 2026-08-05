package com.testcorp.dao;

import java.sql.Connection;
import com.testcorp.db.DbManager6;
import com.testcorp.util.AuditLog;
import com.testcorp.util.StkGeneral;
import com.testcorp.util.Validator;

public class Dao6 {
    private final DbManager6 pool = new DbManager6();

    public String load(String id) throws Exception {
        String out = "";
        try (Connection c = pool.getCon()) {
            java.sql.PreparedStatement ps = c.prepareStatement("SELECT name FROM t WHERE id=?");
            ps.setString(1, id);
            java.sql.ResultSet rs = ps.executeQuery();
            if (rs.next()) { out = rs.getString(1); }
        }
        return out;
    }
    public String load(String id, boolean trace) throws Exception {
        if (trace) { AuditLog.record("dao", id); }
        return load(id);
    }
    public int count() throws Exception {
        return StkGeneral.getStringArray(load("1")).length;
    }
}
