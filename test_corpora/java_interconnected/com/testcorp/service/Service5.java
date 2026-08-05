package com.testcorp.service;

import com.testcorp.dao.Dao5;
import com.testcorp.util.StkGeneral;

public class Service5 {
    private final Dao5 dao = new Dao5();

    public String handle(String id) throws Exception {
        if (StkGeneral.isEmpty(id)) {
            return "";
        }
        String out = dao.load(id);
        return out;
    }

    public String handleTraced(String id) throws Exception {
        return dao.load(id, true);
    }

    public String viaPeer(String id) throws Exception {
        return new Service6().handle(id);
    }
}
