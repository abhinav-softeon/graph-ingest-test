package com.testcorp.service;

import com.testcorp.dao.Dao8;
import com.testcorp.util.StkGeneral;

public class Service8 {
    private final Dao8 dao = new Dao8();

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
        return new Service9().handle(id);
    }
}
