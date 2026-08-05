package com.testcorp.api;

import com.testcorp.service.Service5;

public class HandlerImpl5 implements Handler {
    private final Service5 svc = new Service5();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
