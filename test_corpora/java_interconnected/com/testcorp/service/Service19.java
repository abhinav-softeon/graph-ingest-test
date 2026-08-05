package com.testcorp.service;

import com.testcorp.dao.Dao19;
import com.testcorp.util.StkGeneral;

public class Service19 {
    private final Dao19 dao = new Dao19();

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
        return new Service20().handle(id);
    }
}
