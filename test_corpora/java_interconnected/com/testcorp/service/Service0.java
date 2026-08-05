package com.testcorp.service;

import com.testcorp.dao.Dao0;
import com.testcorp.dao.Dao25;
import com.testcorp.util.StkGeneral;

public class Service0 {
    private final Dao0 dao = new Dao0();
    private final Dao25 dao1 = new Dao25();

    public String handle(String id) throws Exception {
        if (StkGeneral.isEmpty(id)) {
            return "";
        }
        String out = dao.load(id);
        if (out.isEmpty()) { out = handleAlt1(id); }
        return out;
    }

    public String handleTraced(String id) throws Exception {
        return dao.load(id, true);
    }

    public String handleAlt1(String id) throws Exception {
        return dao1.load(id);
    }

    public String viaPeer(String id) throws Exception {
        return new Service1().handle(id);
    }
}
