package com.testcorp.service;

import com.testcorp.dao.Dao1;
import com.testcorp.dao.Dao26;
import com.testcorp.util.StkGeneral;

public class Service1 {
    private final Dao1 dao = new Dao1();
    private final Dao26 dao1 = new Dao26();

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
        return new Service2().handle(id);
    }
}
