package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl9;
import com.testcorp.manager.Manager9;
import com.testcorp.facade.Facade1;
import com.testcorp.service.Service9;

@WebService
public class Endpoint9 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service9().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl9();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager9().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager9().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade1().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade1().orchestrateDirect(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service9 s = new Service9();
        java.util.List<String> one = java.util.Collections.singletonList(id);
        final StringBuilder sb = new StringBuilder();
        one.forEach(v -> {
            try {
                sb.append(s.handle(v));
            } catch (Exception e) {
                sb.append("");
            }
        });
        return sb.toString();
    }

    @WebMethod
    public String viaAnon(final String id) throws Exception {
        Handler h = new Handler() {
            @Override
            public String run(String input) throws Exception {
                return new Service9().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
