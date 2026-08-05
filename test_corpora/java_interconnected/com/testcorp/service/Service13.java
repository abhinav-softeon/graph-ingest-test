package com.testcorp.service;

import com.testcorp.dao.Dao13;
import com.testcorp.util.StkGeneral;

public class Service13 {
    private final Dao13 dao = new Dao13();

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
        return new Service14().handle(id);
    }
}
