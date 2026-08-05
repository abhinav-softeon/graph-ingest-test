package com.testcorp.service;

import com.testcorp.dao.Dao21;
import com.testcorp.util.StkGeneral;

public class Service21 {
    private final Dao21 dao = new Dao21();

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
        return new Service22().handle(id);
    }
}
