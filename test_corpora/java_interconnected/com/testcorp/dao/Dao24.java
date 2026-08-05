package com.testcorp.dao;

import java.sql.Connection;
import com.testcorp.db.DbManager0;
import com.testcorp.util.AuditLog;
import com.testcorp.util.StkGeneral;
import com.testcorp.util.Validator;

public class Dao24 {
    private final DbManager0 pool = new DbManager0();

    public String load(String id) throws Exception {
        String out = "";
        Connection c = null;
        try {
            c = pool.getConnection();
            java.sql.PreparedStatement ps = c.prepareStatement("SELECT name FROM t WHERE id=?");
            ps.setString(1, id);
            java.sql.ResultSet rs = ps.executeQuery();
            if (rs.next()) { out = rs.getString(1); }
        } finally {
            if (c != null) { c.close(); }
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
