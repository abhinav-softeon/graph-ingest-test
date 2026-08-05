package com.testcorp.api;

import com.testcorp.service.Service3;

public class HandlerImpl3 implements Handler {
    private final Service3 svc = new Service3();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
