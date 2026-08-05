package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl0;
import com.testcorp.manager.Manager0;
import com.testcorp.facade.Facade0;
import com.testcorp.service.Service20;
import com.testcorp.service.Service0;

@WebService
public class Endpoint0 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service0().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl0();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager0().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager0().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade0().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade0().orchestrateDirect(id);
    }

    @WebMethod
    public String lookupAlt1(String id) throws Exception {
        return new Service20().handle(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service0 s = new Service0();
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
                return new Service0().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
