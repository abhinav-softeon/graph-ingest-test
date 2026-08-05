package com.testcorp.service;

import com.testcorp.dao.Dao7;
import com.testcorp.util.StkGeneral;

public class Service7 {
    private final Dao7 dao = new Dao7();

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
        return new Service8().handle(id);
    }
}
