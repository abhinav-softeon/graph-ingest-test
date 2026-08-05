package com.testcorp.service;

import com.testcorp.dao.Dao12;
import com.testcorp.util.StkGeneral;

public class Service12 {
    private final Dao12 dao = new Dao12();

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
        return new Service13().handle(id);
    }
}
