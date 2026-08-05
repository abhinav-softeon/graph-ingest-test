package com.testcorp.api;

import com.testcorp.service.Service2;

public class HandlerImpl2 implements Handler {
    private final Service2 svc = new Service2();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
