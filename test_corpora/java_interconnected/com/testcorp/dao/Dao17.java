package com.testcorp.dao;

import java.sql.Connection;
import com.testcorp.db.DbManager1;
import com.testcorp.util.AuditLog;
import com.testcorp.util.StkGeneral;
import com.testcorp.util.Validator;

public class Dao17 {
    private final DbManager1 pool = new DbManager1();

    public String load(String id) throws Exception {
        String out = "";
        Connection c = pool.getDbConn();
            java.sql.Statement st = c.createStatement();
            java.sql.ResultSet rs = st.executeQuery("SELECT name FROM t WHERE id='" + id + "'");
            if (rs.next()) { out = rs.getString(1); }
        c.close();
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
