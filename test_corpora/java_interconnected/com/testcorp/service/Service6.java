package com.testcorp.service;

import com.testcorp.dao.Dao6;
import com.testcorp.util.StkGeneral;

public class Service6 {
    private final Dao6 dao = new Dao6();

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
        return new Service7().handle(id);
    }
}
