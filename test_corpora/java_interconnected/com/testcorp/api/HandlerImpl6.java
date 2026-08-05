package com.testcorp.api;

import com.testcorp.service.Service6;

public class HandlerImpl6 implements Handler {
    private final Service6 svc = new Service6();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
