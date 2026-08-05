package com.testcorp.api;

import com.testcorp.service.Service1;

public class HandlerImpl1 implements Handler {
    private final Service1 svc = new Service1();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
