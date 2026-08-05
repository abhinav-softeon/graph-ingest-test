package com.testcorp.service;

import com.testcorp.dao.Dao24;
import com.testcorp.util.StkGeneral;

public class Service24 {
    private final Dao24 dao = new Dao24();

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
        return new Service0().handle(id);
    }
}
