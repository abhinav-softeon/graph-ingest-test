package com.testcorp.service;

import com.testcorp.dao.Dao17;
import com.testcorp.util.StkGeneral;

public class Service17 {
    private final Dao17 dao = new Dao17();

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
        return new Service18().handle(id);
    }
}
