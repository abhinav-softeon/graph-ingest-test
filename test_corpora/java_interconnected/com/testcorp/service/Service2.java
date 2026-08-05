package com.testcorp.service;

import com.testcorp.dao.Dao2;
import com.testcorp.dao.Dao27;
import com.testcorp.util.StkGeneral;

public class Service2 {
    private final Dao2 dao = new Dao2();
    private final Dao27 dao1 = new Dao27();

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
        return new Service3().handle(id);
    }
}
