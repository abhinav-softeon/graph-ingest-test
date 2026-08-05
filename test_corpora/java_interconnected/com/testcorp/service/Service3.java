package com.testcorp.service;

import com.testcorp.dao.Dao3;
import com.testcorp.dao.Dao28;
import com.testcorp.util.StkGeneral;

public class Service3 {
    private final Dao3 dao = new Dao3();
    private final Dao28 dao1 = new Dao28();

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
        return new Service4().handle(id);
    }
}
