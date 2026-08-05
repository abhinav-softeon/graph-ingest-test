package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl3;
import com.testcorp.manager.Manager3;
import com.testcorp.facade.Facade3;
import com.testcorp.service.Service23;
import com.testcorp.service.Service3;

@WebService
public class Endpoint3 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service3().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl3();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager3().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager3().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade3().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade3().orchestrateDirect(id);
    }

    @WebMethod
    public String lookupAlt1(String id) throws Exception {
        return new Service23().handle(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service3 s = new Service3();
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
                return new Service3().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
