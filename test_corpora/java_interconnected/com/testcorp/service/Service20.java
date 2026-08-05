package com.testcorp.service;

import com.testcorp.dao.Dao20;
import com.testcorp.util.StkGeneral;

public class Service20 {
    private final Dao20 dao = new Dao20();

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
        return new Service21().handle(id);
    }
}
